# Plan: Phase 1 qualitative layer — blending engine + demo

## Context

Phase 0 built all the Claude-notes *plumbing* — the `ClaudeNote` Pydantic schema with a
per-market `qualitative_deltas` discriminated union (`packages/schemas/.../claude_note.py`),
file ingest + watcher + SSE (`api/notes_watcher.py`, `api/events.py`), the
`GET /matches/{id}/notes` endpoint, and a UI panel. **But nothing applies the notes to
predictions** — a codebase-wide search for `log_odds`/`logit`/`blend`/`qualitative` against
the model finds zero hits. The notes are display-only; the actual qualitative *layer* (the
part that adjusts the model's probabilities) is unbuilt, and there are no logit/sigmoid
helpers.

This increment builds the **mathematical heart**: a pure, well-tested function that applies a
note's per-market `log_odds_shift`s to the Dixon-Coles marginals, plus a CLI demo that shows
raw vs blended predictions for a real match. No API/UI changes.

**Hard constraint (accepted): forward-only.** The qualitative layer cannot be honestly
backtested — Claude knows the results of the 2016–2024 matches, so any "historical" note leaks
hindsight and would inflate a backtest meaninglessly. The layer is a forward tool (upcoming
matches); value is assessed prospectively. This plan therefore ships the *engine* and a demo,
not a historical evaluation. Note generation is **Claude-in-the-loop** (a person/subagent
authors `match-<id>.json`); there is no Anthropic SDK/key wired, and that's fine for now.

## Schema being consumed (already exists — do not redefine)

`ClaudeNote.qualitative_deltas: list[QualitativeDelta]`, each a discriminated union member with
`market ∈ {1x2, ou_2_5, btts, corners_total}` and `log_odds_shift: float`. **Sign convention
(from the schema docstring): positive shifts toward `home` / `over` / `yes`.**

## Design

### New module `src/predictor/model/qualitative.py`
- `logit(p)` / `sigmoid(x)` with clamping to `(EPS, 1-EPS)` so extreme marginals don't blow up.
- `apply_qualitative_deltas(marginals: MarketMarginals, deltas: Sequence[QualitativeDelta]) ->
  MarketMarginals` (and a `apply_note(marginals, note)` convenience). Reuses the existing
  `MarketMarginals` dataclass (`model/markets.py:37`). Sum shifts per market first (so the
  result is order-independent), then per market:
  - **1x2** (home/draw/away), δ>0 → toward home: symmetric log-shift on the home–away axis,
    draw fixed in log-space — `p_home ∝ p_home·e^{δ/2}`, `p_away ∝ p_away·e^{−δ/2}`,
    `p_draw` unchanged, then renormalise the triple. (Shifts the home/away log-odds by exactly
    δ while preserving the draw's relative position.)
  - **ou_2_5** (over/under), δ>0 → over: `logit(p_over') = logit(p_over) + δ`; `p_under' =
    1 − p_over'`.
  - **btts** (yes/no), δ>0 → yes: `logit(p_yes') = logit(p_yes) + δ`; `p_no' = 1 − p_yes'`.
  - **corners_total**: not carried by `MarketMarginals` (corners come from a separate Poisson,
    `markets.corner_total_prob_at_least`, and have no odds anyway) → such deltas are **skipped**
    with a logged note. Documented, not silently dropped.
  Each market stays a valid distribution (independent renormalisation). δ=0 is the identity.

### Demo `scripts/qualitative_demo.py`
`uv run python scripts/qualitative_demo.py <match_id> [--d-1x2 X --d-ou Y --d-btts Z]`:
1. Load the match from the DB (teams, kickoff, competition, season).
2. Fit Dixon-Coles (neutral, ridge=2, half-life=540 — the tuned config) on all final matches
   before its kickoff (reuse `dataset.load_training_matches` + the model), `predict` →
   `from_score_matrix` → `MarketMarginals`.
3. Load the real note `match-<id>.json` from `settings.notes_dir` if present (validate with
   `ClaudeNote.model_validate_json`); otherwise build an illustrative note from the `--d-*`
   CLI args so the demo always shows the mechanism.
4. Print raw vs blended marginals per market + the note's summary/deltas, so the qualitative
   shift is visible end-to-end.

## Files
- `src/predictor/model/qualitative.py` — new (logit/sigmoid + blend).
- `scripts/qualitative_demo.py` — new (CLI demo).
- `tests/model/test_qualitative.py` — new.

## Tests (pure, no I/O)
- logit/sigmoid round-trip; clamping keeps p∈(0,1) finite at extreme inputs.
- 1x2: δ>0 raises `p_home`, lowers `p_away`, keeps `p_draw/(p_home·p_away)`-style relative
  draw position, triple sums to 1; the home/away log-odds move by exactly δ.
- ou_2_5 / btts: δ>0 raises over/yes; `logit` moves by exactly δ; pair sums to 1.
- δ=0 is identity; two deltas on one market compound as their sum.
- `corners_total` delta is a no-op on `MarketMarginals` (logged).
- sign convention locked in (positive → home/over/yes).

## Verification
1. `uv run pytest tests/model/test_qualitative.py -q` green.
2. `uv run python scripts/qualitative_demo.py <id> --d-1x2 -0.4 --d-ou 0.3` on a real loaded
   match → shows raw vs blended (home prob drops, over prob rises), proving the end-to-end loop.
3. `make ci` (ruff, mypy --strict, pytest) clean.

## Out of scope (future)
Serving blended predictions via the API/UI (the `/predict` endpoint is enqueue-only and would
first need a real compute-and-persist pipeline); automated Claude-API note generation; any
historical backtest of the layer (leakage — forward-only by nature).

---

## Gemini Review

_Generated: 2026-06-15 02:01:07_

### Summary
The plan introduces a well-defined, pure mathematical layer for applying qualitative probability shifts and a practical CLI demo to visualize the effects. The approach to manipulating log-odds while handling independent renormalization is solid, though the demo script may suffer from performance bottlenecks during model training.

### Issues Found
- **[MEDIUM] Performance bottleneck in demo script**: Fitting the Dixon-Coles model dynamically on *all* historical matches before a kickoff can be computationally expensive and slow (taking seconds or minutes), ruining the fast feedback loop expected of a CLI demo.
- **[LOW] Missing market handling**: The plan does not specify what happens if a note provides a `QualitativeDelta` for a market (e.g., `btts`), but the underlying `MarketMarginals` object lacks base predictions for that market (e.g., if fields can be `None`).
- **[LOW] Demo error handling**: The demo script lacks explicit error handling for missing/invalid `match_id` inputs or cases where the match exists but has insufficient historical data to fit the model.

### Recommendations
- **Optimize demo training**: In `scripts/qualitative_demo.py`, restrict the historical training data to a rolling window (e.g., 3-5 years prior to kickoff) instead of *all* history, or load cached model weights if available, to ensure the demo executes quickly.
- **Handle missing base marginals**: Add logic in `apply_qualitative_deltas` to check if a specific market is populated in `MarketMarginals`. If a base probability is missing (`None`), gracefully skip the corresponding delta and log a warning.
- **Add CLI validations**: Ensure the demo script validates the existence of the `match_id` in the database and exits gracefully with a user-friendly error message if it's not found or if training data is absent.
