"""``OddsPortalSource`` — render + parse OddsPortal into ``OddsRow``s.

Ties :class:`RenderedCache` (browser + disk cache) to the pure parsers. Knows
each tournament's OddsPortal URL slug and its **finals date window**, because a
tournament's ``/results/`` page also lists qualifiers; we paginate (dates run
newest-first) and stop once a page contributes no in-window match.

M1 emits 1X2 (``h2h``) only, parsed from the results-list pages. M2 will add
``totals_2.5`` and ``btts`` from each match's detail sub-pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from predictor.ingest.tournaments import fbref_league_for
from predictor.odds.oddsportal.contracts import OddsRow, ParsedMatch
from predictor.odds.oddsportal.parse import parse_results_list
from predictor.odds.oddsportal.render import RenderedCache

__all__ = ["TOURNAMENTS", "OddsPortalSource", "TournamentSpec"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TournamentSpec:
    """OddsPortal location + DB coordinates + finals date window."""

    name: str
    slug: str  # e.g. "world/world-cup-2018"
    season: str
    window_start: date
    window_end: date

    @property
    def competition(self) -> str:
        return fbref_league_for(self.name)


# Finals windows (inclusive). Note Euro 2020 was played in 2021.
#
# WC 2014 is intentionally absent: OddsPortal gates odds older than ~12 years
# behind login, so its results page renders an empty grid for anonymous users
# (verified 2026-06). Those matches fall back to the backtest's empirical
# baseline. Euro 2016 (10y) is the oldest tournament still served anonymously.
TOURNAMENTS: dict[str, TournamentSpec] = {
    "WC 2018": TournamentSpec(
        "WC 2018", "world/world-cup-2018", "2018", date(2018, 6, 14), date(2018, 7, 15)
    ),
    "Euro 2016": TournamentSpec(
        "Euro 2016", "europe/euro-2016", "2016", date(2016, 6, 10), date(2016, 7, 10)
    ),
    "Euro 2020": TournamentSpec(
        "Euro 2020", "europe/euro-2020", "2020", date(2021, 6, 11), date(2021, 7, 11)
    ),
    "Euro 2024": TournamentSpec(
        "Euro 2024", "europe/euro-2024", "2024", date(2024, 6, 14), date(2024, 7, 14)
    ),
}

_MAX_PAGES = 5


class OddsPortalSource:
    """Render + parse OddsPortal odds for the Phase 0 tournaments."""

    def __init__(self, cache: RenderedCache) -> None:
        self._cache = cache

    def fetch_tournament_odds(self, name: str) -> list[OddsRow]:
        """Return flattened 1X2 ``OddsRow``s for one tournament's finals."""
        spec = TOURNAMENTS[name]
        matches = self._fetch_results(spec)
        rows: list[OddsRow] = []
        for m in matches:
            rows.extend(self._h2h_rows(spec, m))
        logger.info("%s: %d matches in window -> %d h2h odds rows", name, len(matches), len(rows))
        return rows

    def _fetch_results(self, spec: TournamentSpec) -> list[ParsedMatch]:
        """Paginate the results page, keeping only finals-window matches."""
        slug_key = spec.slug.replace("/", "_")
        seen: set[tuple[date, str, str]] = set()
        kept: list[ParsedMatch] = []
        for page in range(1, _MAX_PAGES + 1):
            url = f"https://www.oddsportal.com/football/{spec.slug}/results/#/page/{page}/"
            html = self._cache.get(url, key=f"{slug_key}_results_p{page}")
            parsed = parse_results_list(html)
            if not parsed:
                break
            in_window = [
                m for m in parsed if spec.window_start <= m.match_date <= spec.window_end
            ]
            for m in in_window:
                k = (m.match_date, m.home_team, m.away_team)
                if k not in seen:
                    seen.add(k)
                    kept.append(m)
            # Pages run newest-first; once a page has no in-window match we are
            # past the tournament into qualifiers — stop.
            if not in_window:
                break
        return kept

    @staticmethod
    def _h2h_rows(spec: TournamentSpec, m: ParsedMatch) -> list[OddsRow]:
        def row(outcome: str, odds: float) -> OddsRow:
            return OddsRow(
                competition=spec.competition,
                season=spec.season,
                home_team=m.home_team,
                away_team=m.away_team,
                match_date=m.match_date,
                market="h2h",
                outcome=outcome,
                decimal_odds=odds,
            )

        return [
            row("home", m.home_odds),
            row("draw", m.draw_odds),
            row("away", m.away_odds),
        ]
