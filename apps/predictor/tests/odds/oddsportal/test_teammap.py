"""Team-name normalization tests."""

from __future__ import annotations

import pytest

from predictor.odds.oddsportal.teammap import normalize_team

# OddsPortal name -> expected FBref/DB canonical name.
_CASES = [
    ("USA", "United States"),
    ("South Korea", "Korea Republic"),
    ("Iran", "IR Iran"),
    ("Turkey", "Türkiye"),
    ("North Macedonia", "N. Macedonia"),
    ("Ivory Coast", "Côte d'Ivoire"),
    ("Bosnia and Herzegovina", "Bosnia-Herzegovina"),
    ("Republic of Ireland", "Rep. of Ireland"),
    ("Czech Republic", "Czechia"),
    # Pass-through: names that already match the DB.
    ("Brazil", "Brazil"),
    ("France", "France"),
    ("England", "England"),
]


@pytest.mark.parametrize(("raw", "expected"), _CASES)
def test_normalize_team(raw: str, expected: str) -> None:
    assert normalize_team(raw) == expected


def test_normalize_team_trims_whitespace() -> None:
    assert normalize_team("  Spain  ") == "Spain"
