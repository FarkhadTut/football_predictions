"""Persist OddsPortal ``OddsRow``s into ``odds_snapshots``.

Resolution is **fuzzy** (unlike ``the_odds_api._resolve_match``'s exact tuple):
team names are alias-normalized and matches are found by *same calendar day*
rather than exact kickoff timestamp, because OddsPortal's source data differs
from FBref's on both axes. Home/away may also be swapped between the two
sources (neutral-venue tournaments), so resolution returns an orientation flag
and h2h odds are flipped to align with the DB match's home/away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlmodel import Session, select

from predictor.db.models import Match, OddsSnapshot, Team
from predictor.odds.oddsportal.contracts import OddsRow
from predictor.odds.oddsportal.source import OddsPortalSource
from predictor.odds.oddsportal.teammap import normalize_team

__all__ = ["OddsLoadResult", "load_oddsportal"]

logger = logging.getLogger(__name__)

_BOOK = "oddsportal"
_SWAP = {"home": "away", "away": "home"}


@dataclass(frozen=True)
class OddsLoadResult:
    matches_resolved: int
    matches_unmatched: int
    rows_added: int
    rows_skipped_existing: int


def _team_id(session: Session, name: str) -> int | None:
    return session.exec(select(Team.id).where(Team.name == name)).first()


def _resolve(
    session: Session,
    *,
    competition: str,
    season: str,
    home: str,
    away: str,
    match_date: date,
) -> tuple[int, bool] | None:
    """Resolve to ``(match_id, swap)`` or ``None``.

    Matches on the *unordered* team pair within ``(competition, season)``;
    ``match_date`` only breaks ties when a pair meets more than once in a
    tournament (e.g. group stage + final). Date is deliberately not required:
    OddsPortal renders venue-local dates (off-by-one vs the stored UTC date) and
    occasionally lists a match under a second, wrong date — both of which would
    otherwise drop an odds row.

    ``swap`` is ``True`` when OddsPortal's home/away are reversed relative to the
    stored match, so the caller flips h2h ``home``/``away`` outcomes.
    """
    home_id = _team_id(session, normalize_team(home))
    away_id = _team_id(session, normalize_team(away))
    if home_id is None or away_id is None:
        return None
    candidates = list(
        session.exec(
            select(Match.id, Match.home_team_id, Match.kickoff_utc).where(
                Match.competition == competition,
                Match.season == season,
                Match.home_team_id.in_((home_id, away_id)),  # type: ignore[attr-defined]
                Match.away_team_id.in_((home_id, away_id)),  # type: ignore[attr-defined]
            )
        ).all()
    )
    if not candidates:
        return None
    # Tie-break a repeated pairing (group + knockout) by closest kickoff date.
    best = min(candidates, key=lambda r: abs((r[2].date() - match_date).days))
    match_id, db_home_id, _ = best
    if match_id is None:
        return None
    return match_id, (db_home_id != home_id)


def _exists(
    session: Session, *, match_id: int, market: str, outcome: str, fetched_at: datetime
) -> bool:
    stmt = select(OddsSnapshot.id).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.book == _BOOK,
        OddsSnapshot.market == market,
        OddsSnapshot.outcome == outcome,
        OddsSnapshot.fetched_at == fetched_at,
    )
    return session.exec(stmt).first() is not None


def _group_key(r: OddsRow) -> tuple[str, str, str, str, date]:
    return (r.competition, r.season, r.home_team, r.away_team, r.match_date)


def load_oddsportal(
    session: Session,
    source: OddsPortalSource,
    name: str,
    *,
    fetched_at: datetime,
) -> OddsLoadResult:
    """Render/parse one tournament and upsert its odds into ``odds_snapshots``.

    Idempotent: rows matching the natural key
    ``(match_id, book, market, outcome, fetched_at)`` are skipped. Unresolved
    matches are logged and counted, never fatal. Caller need not commit — this
    commits on success.
    """
    rows = source.fetch_tournament_odds(name)

    # Group rows by match so each match is resolved once.
    by_match: dict[tuple[str, str, str, str, date], list[OddsRow]] = {}
    for r in rows:
        by_match.setdefault(_group_key(r), []).append(r)

    resolved = unmatched = added = skipped = 0
    for (competition, season, home, away, match_date), group in by_match.items():
        target = _resolve(
            session,
            competition=competition,
            season=season,
            home=home,
            away=away,
            match_date=match_date,
        )
        if target is None:
            unmatched += 1
            logger.warning(
                "oddsportal unmatched: %s %s %s vs %s on %s",
                competition,
                season,
                home,
                away,
                match_date,
            )
            continue
        match_id, swap = target
        resolved += 1
        for r in group:
            outcome = _SWAP.get(r.outcome, r.outcome) if swap else r.outcome
            if _exists(
                session,
                match_id=match_id,
                market=r.market,
                outcome=outcome,
                fetched_at=fetched_at,
            ):
                skipped += 1
                continue
            session.add(
                OddsSnapshot(
                    match_id=match_id,
                    book=_BOOK,
                    market=r.market,
                    outcome=outcome,
                    decimal_odds=r.decimal_odds,
                    fetched_at=fetched_at,
                )
            )
            added += 1

    session.commit()
    return OddsLoadResult(
        matches_resolved=resolved,
        matches_unmatched=unmatched,
        rows_added=added,
        rows_skipped_existing=skipped,
    )
