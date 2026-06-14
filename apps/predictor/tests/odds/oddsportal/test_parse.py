"""Parser tests against a real rendered OddsPortal results page (offline).

Fixture ``wc2018_results_p1.html`` is a snapshot of the rendered DOM for
``/football/world/world-cup-2018/results/`` page 1 — no network needed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from predictor.odds.oddsportal.parse import parse_results_list

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "oddsportal"


def _html() -> str:
    return (FIXTURES / "wc2018_results_p1.html").read_text(encoding="utf-8")


def test_parse_results_list_returns_matches_with_valid_1x2() -> None:
    matches = parse_results_list(_html())
    # ~50 matches per page; responsive duplicates collapsed.
    assert len(matches) >= 45
    for m in matches:
        assert m.home_team and m.away_team
        assert m.home_odds > 1.0 and m.draw_odds > 1.0 and m.away_odds > 1.0


def test_parse_results_list_extracts_known_final() -> None:
    matches = {(m.home_team, m.away_team): m for m in parse_results_list(_html())}
    final = matches[("France", "Croatia")]
    assert final.match_date == date(2018, 7, 15)
    assert (final.home_odds, final.draw_odds, final.away_odds) == (2.02, 2.98, 4.88)
    # Score is best-effort but should be the real 4-2.
    assert (final.home_score, final.away_score) == (4, 2)
    assert final.detail_path is not None and final.detail_path.startswith("/football/")


def test_parse_results_list_no_duplicate_keys() -> None:
    matches = parse_results_list(_html())
    keys = [(m.match_date, m.home_team, m.away_team) for m in matches]
    assert len(keys) == len(set(keys))
