"""Live ``FBrefTournamentSource`` adapter wrapping ``soccerdata.FBref``.

Translates the multi-index DataFrames soccerdata returns into the
source-neutral :class:`ScheduleRow` / :class:`TeamMatchStatRow` rows the
``predictor.ingest.tournaments`` loader expects.

Two soccerdata endpoints:

* ``FBref.read_schedule()`` — one row per match. Columns include
  ``date``, ``time``, ``home_team``, ``away_team``, ``score``. Score is a
  string like ``"2\u20131"`` (en-dash) when played, NaN when scheduled.
* ``FBref.read_team_match_stats(stat_type='misc')`` — one row per
  team-match with a ``CrdY`` / ``CrdR`` / ``Fls`` group; corners live in
  the ``"performance" / "CK"`` (or ``"Performance" / "CK"``) column when
  available, otherwise we fall back to ``stat_type='passing'`` which
  exposes ``CK`` on newer seasons. Older internationals (WC 2014 / Euro
  2016) often lack corners — those rows are emitted with ``corners=None``
  and the dataset adapter then skips the corners market for them.

The adapter stores ``competition`` = the FBref league id
(``"INT-World Cup"`` / ``"INT-European Championship"``) so downstream
modules can use a stable identifier independent of the friendly name.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, time
from typing import TYPE_CHECKING, Any

from predictor.ingest.contracts import ScheduleRow, TeamMatchStatRow
from predictor.ingest.tournaments import fbref_league_for

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["FBrefTournamentSource", "enable_offline_cache"]

logger = logging.getLogger(__name__)

_OFFLINE_PATCHED = False


def enable_offline_cache() -> None:
    """Patch ``soccerdata`` to serve only from its local HTML cache.

    FBref sits behind Cloudflare; ``soccerdata.FBref`` is a
    ``BaseSeleniumReader`` that (a) eagerly launches an undetected-Chrome
    driver in ``__init__`` to solve the challenge and (b) re-downloads any
    cache file older than its default ``MAXAGE``. From a blocked IP both
    paths hang indefinitely (a hang is not a ``WebDriverException``, so the
    constructor's own guard never fires).

    This installs two idempotent monkeypatches so cached reads work with
    zero network:

    * ``BaseSeleniumReader._init_webdriver`` becomes a no-op — no Chrome is
      launched. Cached reads never touch the driver, so this is safe.
    * ``BaseReader.get`` serves any existing cache file regardless of age
      and raises ``FileNotFoundError`` fast when a file is missing, instead
      of falling through to the (hanging) Selenium download.

    Call before constructing any ``soccerdata`` reader. A genuinely
    uncached season then surfaces as a clean ``FileNotFoundError`` rather
    than a hang.
    """
    global _OFFLINE_PATCHED
    if _OFFLINE_PATCHED:
        return
    import soccerdata._common as common

    def _offline_get(
        self: Any,
        url: str,
        filepath: Any = None,
        max_age: Any = None,
        no_cache: bool = False,
        var: Any = None,
    ) -> Any:
        if filepath is not None and filepath.exists():
            return filepath.open(mode="rb")
        raise FileNotFoundError(f"offline cache miss for {url} -> {filepath}")

    common.BaseSeleniumReader._init_webdriver = lambda self: None
    common.BaseReader.get = _offline_get
    _OFFLINE_PATCHED = True
    logger.info("soccerdata offline cache mode enabled (no network, cache-only)")


def _parse_score(raw: object) -> tuple[int | None, int | None]:
    """Parse FBref's score column.

    Returns ``(home_goals, away_goals)`` or ``(None, None)`` when the
    match hasn't been played (NaN) or the string is malformed.
    """
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None, None
    # FBref uses an en-dash; tolerate ASCII hyphen too.
    for sep in ("\u2013", "-", ":"):
        if sep in s:
            left, right = s.split(sep, 1)
            try:
                # Trim any "(pen)" / shootout suffixes off the right half.
                right_main = right.strip().split(" ")[0]
                return int(left.strip()), int(right_main)
            except ValueError:
                return None, None
    return None, None


def _combine_date_time(date_val: object, time_val: object) -> datetime | None:
    """Combine a date (``YYYY-MM-DD`` or datetime) + optional ``HH:MM`` time.

    FBref times are local to the venue but soccerdata doesn't carry the
    timezone; we treat the combined timestamp as UTC. For Phase 0 the
    Brier gate is invariant to within-day kickoff shifts, so the loss is
    acceptable.
    """
    if date_val is None:
        return None
    import pandas as pd  # local import keeps the module light to type-check

    if pd.isna(date_val):
        return None
    if isinstance(date_val, datetime):
        d = date_val.date()
    else:
        try:
            d = pd.to_datetime(date_val).date()
        except (ValueError, TypeError):
            return None
    t = time(0, 0)
    if time_val is not None and not pd.isna(time_val):
        s = str(time_val).strip()
        # FBref times look like "19:00" or "19:00 (20:00)" (local + UK).
        head = s.split(" ")[0]
        try:
            hh, mm = head.split(":")
            t = time(int(hh), int(mm))
        except (ValueError, IndexError):
            pass
    return datetime.combine(d, t)


def _pick_column(df: pd.DataFrame, *candidates: str | tuple[str, ...]) -> Any | None:
    """Return the first column from ``candidates`` present in ``df``.

    Supports both flat indexes (str) and MultiIndex columns (tuple).
    """
    for c in candidates:
        if c in df.columns:
            return df[c]
    return None


def _int_or_none(v: object) -> int | None:
    import pandas as pd

    if v is None or pd.isna(v):
        return None
    try:
        return int(v)  # type: ignore[call-overload,no-any-return]
    except (ValueError, TypeError):
        return None


def _str_or_none(v: object) -> str | None:
    import pandas as pd

    if v is None or pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


class FBrefTournamentSource:
    """Production adapter that calls ``soccerdata.FBref`` lazily.

    Parameters
    ----------
    no_cache:
        Forwarded to ``sd.FBref`` — set ``True`` to bypass the disk cache
        when re-running after a soccerdata upgrade.
    stat_types:
        Iterable of stat types attempted in order when searching for
        corners. Defaults to ``("misc", "passing")``.
    offline:
        When ``True``, patch ``soccerdata`` to read only from its local
        HTML cache (no Chrome, no network). Required when FBref is
        Cloudflare-blocked from this IP; an uncached season then raises
        ``FileNotFoundError`` instead of hanging. See
        :func:`enable_offline_cache`.
    """

    def __init__(
        self,
        *,
        no_cache: bool = False,
        stat_types: Iterable[str] = ("misc", "passing"),
        offline: bool = False,
    ) -> None:
        self._no_cache = no_cache
        self._stat_types = tuple(stat_types)
        self._offline = offline
        if offline:
            enable_offline_cache()

    def _fbref(self, name: str, season: str) -> Any:
        import soccerdata as sd

        league = fbref_league_for(name)
        kwargs: dict[str, Any] = {"leagues": league, "seasons": season}
        if self._no_cache:
            kwargs["no_cache"] = True
        return sd.FBref(**kwargs)

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def fetch_schedule(self, name: str, season: str) -> list[ScheduleRow]:
        fbref = self._fbref(name, season)
        df = fbref.read_schedule()
        df = df.reset_index() if df.index.nlevels else df
        competition = fbref_league_for(name)
        rows: list[ScheduleRow] = []
        for _, raw in df.iterrows():
            home = _str_or_none(raw.get("home_team"))
            away = _str_or_none(raw.get("away_team"))
            if home is None or away is None:
                continue
            kickoff = _combine_date_time(raw.get("date"), raw.get("time"))
            if kickoff is None:
                continue
            hg, ag = _parse_score(raw.get("score"))
            rows.append(
                ScheduleRow(
                    competition=competition,
                    season=season,
                    home_team=home,
                    away_team=away,
                    kickoff_utc=kickoff,
                    home_goals=hg,
                    away_goals=ag,
                )
            )
        return rows

    # ------------------------------------------------------------------
    # Team match stats (corners)
    # ------------------------------------------------------------------

    def fetch_team_match_stats(self, name: str, season: str) -> list[TeamMatchStatRow]:
        fbref = self._fbref(name, season)
        # Schedule needed for natural keys: each stats row carries
        # (date, home_team, away_team, team) but not always all three —
        # we reconstruct home/away from the schedule frame.
        schedule_df = fbref.read_schedule().reset_index()
        # Build a lookup: (date, home_team, away_team) → ScheduleRow shape
        # so we can attach corners stats back to the natural key.
        sched_rows: list[ScheduleRow] = self.fetch_schedule(name, season)
        if not sched_rows:
            return []
        sched_by_date_pair: dict[tuple[str, str, str], ScheduleRow] = {}
        for s in sched_rows:
            key = (s.kickoff_utc.date().isoformat(), s.home_team, s.away_team)
            sched_by_date_pair[key] = s

        competition = fbref_league_for(name)
        out: dict[tuple[str, str, str, str], TeamMatchStatRow] = {}

        for stat_type in self._stat_types:
            try:
                df = fbref.read_team_match_stats(stat_type=stat_type)
            except (ValueError, KeyError) as exc:
                logger.info("fbref stat_type=%s unavailable: %s", stat_type, exc)
                continue
            except Exception as exc:
                logger.warning("fbref stat_type=%s failed: %s", stat_type, exc)
                continue
            df = df.reset_index() if df.index.nlevels else df
            self._merge_stats(
                df=df,
                schedule_df=schedule_df,
                competition=competition,
                season=season,
                sched_by_date_pair=sched_by_date_pair,
                out=out,
            )

        return list(out.values())

    @staticmethod
    def _merge_stats(
        *,
        df: pd.DataFrame,
        schedule_df: pd.DataFrame,
        competition: str,
        season: str,
        sched_by_date_pair: dict[tuple[str, str, str], ScheduleRow],
        out: dict[tuple[str, str, str, str], TeamMatchStatRow],
    ) -> None:
        """Flatten one stats DataFrame and fold into ``out``.

        FBref tends to return MultiIndex columns — flatten by taking the
        last non-empty level. The ``team`` column is always present.
        """
        import pandas as pd

        flat_cols: dict[str, Any] = {}
        if isinstance(df.columns, pd.MultiIndex):
            for raw_col in df.columns:
                parts = [str(p) for p in raw_col if str(p) and str(p) != "nan"]
                key = parts[-1] if parts else str(raw_col)
                # First write wins so a flat name doesn't get overwritten
                # by a less-informative duplicate.
                flat_cols.setdefault(key, raw_col)
        else:
            for raw_col in df.columns:
                flat_cols[str(raw_col)] = raw_col

        def col(name: str) -> Any | None:
            actual = flat_cols.get(name)
            if actual is None:
                return None
            return df[actual]

        team_col = col("team")
        if team_col is None:
            return
        date_col = col("date") if "date" in flat_cols else None
        # Some stat frames carry game_id only — reconstruct date from schedule.
        for idx in df.index:
            team = _str_or_none(team_col.iloc[idx] if hasattr(team_col, "iloc") else team_col[idx])
            if team is None:
                continue
            # Resolve (date, home, away):
            home_v = col("home_team")
            away_v = col("away_team")
            date_v = date_col
            # Some frames have an "opponent" column instead — we then
            # need the schedule frame for the home/away pair.
            if home_v is not None and away_v is not None and date_v is not None:
                home = _str_or_none(home_v.iloc[idx])
                away = _str_or_none(away_v.iloc[idx])
                date_raw = date_v.iloc[idx]
            else:
                game_id_col = col("game_id")
                if game_id_col is None:
                    continue
                gid = _str_or_none(game_id_col.iloc[idx])
                if gid is None:
                    continue
                match_rows = schedule_df[schedule_df["game_id"] == gid]
                if match_rows.empty:
                    continue
                row = match_rows.iloc[0]
                home = _str_or_none(row.get("home_team"))
                away = _str_or_none(row.get("away_team"))
                date_raw = row.get("date")
            if home is None or away is None or date_raw is None or pd.isna(date_raw):
                continue
            try:
                date_str = pd.to_datetime(date_raw).date().isoformat()
            except (ValueError, TypeError):
                continue
            key_sched = (date_str, home, away)
            if key_sched not in sched_by_date_pair:
                continue

            sched = sched_by_date_pair[key_sched]
            out_key = (date_str, home, away, team)
            existing = out.get(out_key)
            # Pull individual fields, preferring fresh values over previous None.
            # ``row_idx`` is bound as a default so the closure captures this
            # iteration's index (B023), though it is only ever called inline.
            def _cell(name: str, row_idx: int = idx) -> int | None:
                series = col(name)
                if series is None:
                    return None
                return _int_or_none(series.iloc[row_idx])

            shots = _cell("Sh")
            sot = _cell("SoT")
            corners = _cell("CK")
            yel = _cell("CrdY")
            red = _cell("CrdR")
            fouls = _cell("Fls")

            merged = TeamMatchStatRow(
                competition=competition,
                season=season,
                home_team=home,
                away_team=away,
                kickoff_utc=sched.kickoff_utc,
                team=team,
                shots=shots if shots is not None else (existing.shots if existing else None),
                shots_on_target=sot
                if sot is not None
                else (existing.shots_on_target if existing else None),
                corners=corners
                if corners is not None
                else (existing.corners if existing else None),
                yellow_cards=yel
                if yel is not None
                else (existing.yellow_cards if existing else None),
                red_cards=red if red is not None else (existing.red_cards if existing else None),
                fouls=fouls if fouls is not None else (existing.fouls if existing else None),
            )
            out[out_key] = merged
