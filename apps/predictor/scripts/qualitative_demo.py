"""Demo: show raw vs Claude-blended predictions for one match.

Fits the tuned Dixon-Coles model up to a match's kickoff, derives market
marginals, then applies the match's Claude note (``match-<id>.json`` in
``settings.notes_dir`` if present, else an illustrative note built from the
``--d-*`` flags) and prints the before/after.

    uv run python scripts/qualitative_demo.py <match_id> --d-1x2 -0.4 --d-ou 0.3
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

import pandas as pd
from predictor_schemas import ClaudeNote
from predictor_schemas.claude_note import (
    Delta1x2,
    DeltaBTTS,
    DeltaOU25,
    QualitativeDelta,
)
from sqlmodel import select

from predictor.backtest.dataset import load_training_matches
from predictor.config import get_settings
from predictor.db.models import Match, Team
from predictor.db.session import get_session
from predictor.model.dixon_coles import DixonColesModel
from predictor.model.markets import MarketMarginals, from_score_matrix
from predictor.model.qualitative import apply_note

logging.basicConfig(level=logging.WARNING)

# Tuned config (matches the backtest defaults).
_NEUTRAL = True
_RIDGE = 2.0
_HALF_LIFE = 540.0


def _load_match(match_id: int) -> tuple[str, str, datetime]:
    with get_session() as s:
        row = s.exec(
            select(Match, Team.name).where(Match.id == match_id, Match.home_team_id == Team.id)
        ).first()
        if row is None:
            raise SystemExit(f"match {match_id} not found")
        match, home = row
        away = s.exec(select(Team.name).where(Team.id == match.away_team_id)).first()
    if away is None:
        raise SystemExit(f"match {match_id} has no away team")
    return home, away, match.kickoff_utc


def _fit_marginals(home: str, away: str, kickoff: datetime) -> MarketMarginals:
    corpus = load_training_matches(held_out=())
    train = corpus.loc[pd.to_datetime(corpus["kickoff_utc"]) < pd.Timestamp(kickoff)]
    if train.empty:
        raise SystemExit(f"no training data before {kickoff}")
    model = DixonColesModel(half_life_days=_HALF_LIFE, neutral_venue=_NEUTRAL)
    model.fit(train, as_of=kickoff, ridge=_RIDGE)
    assert model.params is not None
    if home not in model.params.teams or away not in model.params.teams:
        raise SystemExit(f"model has not seen {home!r} and/or {away!r} before this match")
    return from_score_matrix(model.predict(home, away))


def _note_for(match_id: int, args: argparse.Namespace) -> ClaudeNote:
    path = get_settings().notes_dir / f"match-{match_id}.json"
    if path.exists():
        return ClaudeNote.model_validate_json(path.read_bytes())
    deltas: list[QualitativeDelta] = []
    if args.d_1x2 is not None:
        deltas.append(Delta1x2(log_odds_shift=args.d_1x2))
    if args.d_ou is not None:
        deltas.append(DeltaOU25(log_odds_shift=args.d_ou))
    if args.d_btts is not None:
        deltas.append(DeltaBTTS(log_odds_shift=args.d_btts))
    return ClaudeNote(
        match_id=match_id,
        created_at=datetime.now(UTC),
        summary="(illustrative note from --d-* flags; no real note file found)",
        qualitative_deltas=deltas,
        confidence=0.5,
        sources=[],
    )


def _print_pair(label: str, outcomes: list[tuple[str, float, float]]) -> None:
    print(f"\n{label}")
    print(f"  {'outcome':<7} {'raw':>7} {'blended':>8}  Δ")
    for name, raw, blended in outcomes:
        print(f"  {name:<7} {raw:>7.3f} {blended:>8.3f}  {blended - raw:+.3f}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="qualitative_demo")
    p.add_argument("match_id", type=int)
    p.add_argument("--d-1x2", type=float, default=None, dest="d_1x2", help="log-odds shift → home")
    p.add_argument("--d-ou", type=float, default=None, dest="d_ou", help="log-odds shift → over")
    p.add_argument("--d-btts", type=float, default=None, dest="d_btts", help="log-odds shift → yes")
    args = p.parse_args(argv)

    home, away, kickoff = _load_match(args.match_id)
    raw = _fit_marginals(home, away, kickoff)
    note = _note_for(args.match_id, args)
    blended = apply_note(raw, note)

    print(f"Match {args.match_id}: {home} vs {away}  ({kickoff:%Y-%m-%d})")
    print(f"Note: {note.summary}")
    print(f"Deltas: {[(d.market, round(d.log_odds_shift, 3)) for d in note.qualitative_deltas]}")
    _print_pair(
        "1X2",
        [
            ("home", raw.p_home, blended.p_home),
            ("draw", raw.p_draw, blended.p_draw),
            ("away", raw.p_away, blended.p_away),
        ],
    )
    _print_pair(
        "O/U 2.5",
        [("over", raw.p_over_2_5, blended.p_over_2_5), ("under", raw.p_under_2_5, blended.p_under_2_5)],
    )
    _print_pair(
        "BTTS",
        [("yes", raw.p_btts_yes, blended.p_btts_yes), ("no", raw.p_btts_no, blended.p_btts_no)],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
