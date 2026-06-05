"""Unit tests for the WC 2026 squad heuristic.

The heuristic is a pure function on caps + league-activity data, so
tests inject a ``FakeSource`` and assert candidate inclusion/exclusion
against representative edge cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from predictor.ingest.squad_heuristic import (
    TOP5_LEAGUES,
    CapRecord,
    LeagueActivityRecord,
    candidates_for,
)


@dataclass
class FakeCapsSource:
    caps: list[CapRecord] = field(default_factory=list)
    activity: list[LeagueActivityRecord] = field(default_factory=list)

    def fetch_caps(self, nation: str, since: datetime) -> list[CapRecord]:
        return [c for c in self.caps if c.cap_date >= since]

    def fetch_league_activity(self, nation: str, since: datetime) -> list[LeagueActivityRecord]:
        return list(self.activity)


AS_OF = datetime(2026, 6, 1)


def _cap(
    name: str, *, nation: str = "Brazil", days_ago: int, position: str | None = None
) -> CapRecord:
    return CapRecord(
        player_name=name,
        nation=nation,
        cap_date=AS_OF - timedelta(days=days_ago),
        player_fbref_id=f"p-{name.lower()}",
        position=position,
    )


def _activity(
    name: str,
    *,
    nation: str = "Brazil",
    league: str = "ENG-Premier League",
    starts: int = 20,
    position: str | None = None,
) -> LeagueActivityRecord:
    return LeagueActivityRecord(
        player_name=name,
        nation=nation,
        league=league,
        starts=starts,
        player_fbref_id=f"p-{name.lower()}",
        position=position,
    )


def test_top5_leagues_match_soccerdata_ids() -> None:
    assert (
        frozenset(
            {
                "ENG-Premier League",
                "ESP-La Liga",
                "GER-Bundesliga",
                "ITA-Serie A",
                "FRA-Ligue 1",
            }
        )
        == TOP5_LEAGUES
    )


def test_player_with_three_caps_in_window_is_included() -> None:
    source = FakeCapsSource(
        caps=[
            _cap("Vinicius", days_ago=10, position="LW"),
            _cap("Vinicius", days_ago=120),
            _cap("Vinicius", days_ago=200),
        ]
    )
    result = candidates_for(source, "Brazil", AS_OF)
    assert len(result) == 1
    assert result[0].player_name == "Vinicius"
    assert "caps in trailing 12mo" in result[0].rationale
    assert result[0].position == "LW"  # picked up from the first cap


def test_player_with_two_caps_only_is_excluded_without_top5_activity() -> None:
    source = FakeCapsSource(caps=[_cap("Casemiro", days_ago=10), _cap("Casemiro", days_ago=120)])
    assert candidates_for(source, "Brazil", AS_OF) == []


def test_top5_starter_with_one_cap_is_included() -> None:
    source = FakeCapsSource(
        caps=[_cap("Rodrygo", days_ago=200)],
        activity=[_activity("Rodrygo", league="ESP-La Liga", starts=18)],
    )
    result = candidates_for(source, "Brazil", AS_OF)
    assert len(result) == 1
    assert "ESP-La Liga" in result[0].rationale
    assert "1 cap" in result[0].rationale


def test_top5_starter_with_zero_caps_is_excluded() -> None:
    """Rule (b) requires at least one cap."""
    source = FakeCapsSource(activity=[_activity("Endrick", league="ESP-La Liga", starts=25)])
    assert candidates_for(source, "Brazil", AS_OF) == []


def test_non_top5_league_does_not_count() -> None:
    source = FakeCapsSource(
        caps=[_cap("Player", days_ago=30)],
        activity=[_activity("Player", league="POR-Primeira Liga", starts=30)],
    )
    assert candidates_for(source, "Brazil", AS_OF) == []


def test_caps_outside_window_are_ignored() -> None:
    source = FakeCapsSource(
        caps=[
            _cap("OldPlayer", days_ago=400),
            _cap("OldPlayer", days_ago=500),
            _cap("OldPlayer", days_ago=600),
        ]
    )
    assert candidates_for(source, "Brazil", AS_OF) == []


def test_other_nation_caps_are_filtered() -> None:
    source = FakeCapsSource(
        caps=[
            _cap("ArgentinaPlayer", nation="Argentina", days_ago=30),
            _cap("ArgentinaPlayer", nation="Argentina", days_ago=120),
            _cap("ArgentinaPlayer", nation="Argentina", days_ago=200),
        ]
    )
    assert candidates_for(source, "Brazil", AS_OF) == []


def test_results_are_sorted_by_player_name() -> None:
    source = FakeCapsSource(
        caps=[
            _cap("Vinicius", days_ago=10),
            _cap("Vinicius", days_ago=120),
            _cap("Vinicius", days_ago=200),
            _cap("Casemiro", days_ago=10),
            _cap("Casemiro", days_ago=120),
            _cap("Casemiro", days_ago=200),
        ]
    )
    result = candidates_for(source, "Brazil", AS_OF)
    assert [c.player_name for c in result] == ["Casemiro", "Vinicius"]


def test_min_starts_zero_in_top5_excluded() -> None:
    source = FakeCapsSource(
        caps=[_cap("Bench", days_ago=10)],
        activity=[_activity("Bench", league="ENG-Premier League", starts=0)],
    )
    assert candidates_for(source, "Brazil", AS_OF) == []


@pytest.mark.parametrize("league", sorted(TOP5_LEAGUES))
def test_each_top5_league_qualifies(league: str) -> None:
    source = FakeCapsSource(
        caps=[_cap("Player", days_ago=30)],
        activity=[_activity("Player", league=league, starts=15)],
    )
    result = candidates_for(source, "Brazil", AS_OF)
    assert len(result) == 1
    assert league in result[0].rationale
