# Plan: Improve the model toward beating the market

## Context

The Phase 0 Dixon-Coles model loses to the OddsPortal closing-odds baseline on every
market (pooled `1x2` 1.34×, `ou_2_5` 1.27×, `btts` 1.17× — ratio >1 means worse Brier).
A 34% gap on 1X2 is far worse than a competent DC model should be — it points to
**systematic miscalibration**, not just an efficient market. Diagnosis (read of
`model/dixon_coles.py` + `backtest/run.py`):

1. **Home advantage applied at neutral venues.** `predict_lambdas` always adds the global
   `home_adv` γ (≈0.25, a ~28% boost to the nominal home team's λ; `dixon_coles.py:373`).
   Every loaded match is an international tournament on neutral ground where home/away is
   arbitrary, so this inflates the "home" side's win probability on *every* prediction.
2. **No regularization** (`fit` is pure MLE, `dixon_coles.py:284-367`). Per fold, teams have
   ~3–7 matches, so attack/defence overfit → overconfident probabilities → Brier penalises
   the tails heavily.

Both are fixable with the data already in the DB. **Honest expectation:** these should close
most of the gap toward parity (and may beat the market on the markets where it's currently
closest — e.g. BTTS, already <1 in Euro 2016 + WC 2018 per-tournament). Consistently beating
*closing* odds on ≥3 markets is a high bar; if these high-leverage fixes don't clear 0.98,
that's strong evidence we need the richer signal (club-form / Phase 1 qualitative layer)
rather than more goal-model tuning. The deliverable is the lowest pooled Brier we can reach on
existing data, with a clear pass/fail per market.

## Approach (experiment-driven: implement lever → measure on the gate → keep if it helps)

### Change 1 — Neutral-venue mode (highest leverage)
Add `neutral_venue: bool = False` to `DixonColesModel` (`model/dixon_coles.py`).
- When `True`: do **not** estimate or apply γ. `λ_home = exp(α_home + δ_away)`,
  `λ_away = exp(α_away + δ_home)` — fully symmetric (the nominal home/away labels carry no
  advantage). Drop γ from the parameter vector (or fix it to 0 and skip its gradient).
- Migration-free: 100% of current data is neutral, so a model-level flag suffices. The
  backtest constructs the model with `neutral_venue=True`. (When non-neutral club data is
  added later, upgrade to a per-match `neutral` flag — noted as future work, not done now.)

### Change 2 — Ridge regularization on team strengths
Add `ridge: float = 0.0` to `fit`. Add `0.5 * ridge * (Σα² + Σδ²)` to the negative
log-likelihood and `ridge·α`, `ridge·δ` to the analytic gradient (the gradient is already
hand-coded at `dixon_coles.py:227-243`). Shrinks sparse-data teams toward the league-average
(α=δ=0 after the existing re-centering at `:351-357`) → calibrated, less overconfident.

### Change 3 — Wire the knobs through the backtest + tune
- `run_walk_forward` / `_main` (`backtest/run.py`): pass `neutral_venue=True` and a `ridge`
  value into the per-fold `DixonColesModel(...)`. Expose `--ridge` (and reuse existing
  `--half-life-days`) on the CLI.
- Add a small sweep script `scripts/tune_model.py`: run the walk-forward over a grid of
  `ridge ∈ {0, 0.5, 1, 2, 5, 10}` × `half_life_days ∈ {None, 365, 540}` (with
  `neutral_venue=True`), print the pooled Brier ratio per market for each, and report the
  config that minimises the pooled ratio across the 3 markets. Reuses
  `dataset.load_training_matches`/`load_test_matches` and `run_walk_forward`/`check`.

## Files
- `src/predictor/model/dixon_coles.py` — `neutral_venue` (fit + predict_lambdas), `ridge`
  penalty in the objective + gradient, drop/zero γ when neutral.
- `src/predictor/backtest/run.py` — thread `neutral_venue` + `ridge` into the fold model;
  add `--ridge` CLI arg.
- `scripts/tune_model.py` — grid sweep over (ridge, half_life), reports best by pooled Brier.
- Tests: `tests/model/test_dixon_coles.py` — (a) neutral mode makes `predict(A,B)` and the
  swapped `predict(B,A)` symmetric (home prob == away prob of the swap); (b) γ not applied in
  neutral mode; (c) ridge shrinks the attack/defence spread vs unregularized on the same data.

## Verification
1. Baseline first: `uv run python -m predictor.backtest.run` → record current ratios
   (1.34 / 1.27 / 1.17).
2. Apply Change 1, re-run → expect a large 1X2 improvement (neutral removes the home bias).
3. `uv run python scripts/tune_model.py` → pick best (ridge, half_life); set as the CLI
   defaults; regenerate `reports/backtest-phase0.md`.
4. Report final pooled ratios + per-market pass/fail vs the 0.98 gate. State plainly which
   markets (if any) beat the market and whether the REQ-007 gate (≥3 of 4) passes.
5. `uv run pytest tests/model tests/backtest -q` and `make ci` green.

## Out of scope (future escalation if parity isn't enough)
Club-form ingestion (needs an FBref club adapter; FBref is Cloudflare-blocked — depends on a
soccerdata club cache that may not exist) and the Phase 1 Claude qualitative layer. These are
where real edge over a closing line would most plausibly come from, but they're large and
data-dependent — revisit only if Changes 1–3 stall short of the gate.
