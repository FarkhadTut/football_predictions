# Brainstorm: Football Match Outcome Predictor (WC 2026)

**Date**: 2026-06-03
**Type**: Project-related
**Session depth**: Deep dive

---

## Topic Overview

A two-layer prediction system for FIFA World Cup 2026:

1. **Statistical layer**: hierarchical Poisson / Dixon-Coles model trained on club football involving WC squad players, with a tournament-context adjustment layer.
2. **Qualitative layer**: terminal Claude (this assistant — *not* the API) manually scrapes pre-match news, lineups, injuries, presser sentiment, etc. and emits structured deltas + a written narrative.
3. **Combiner**: Bayesian blend of (1) and (2) yields per-market posterior probabilities.
4. **Accumulator builder**: turns per-match posteriors + scraped bookmaker odds into ranked N-leg accumulator suggestions targeting user-defined payout (e.g. x2000).

The user manually places bets at 1xbet; the app never automates placement.

---

## Context

- **Timeline**: WC 2026 starts **2026-06-11** — 8 days from this brainstorm. Tournament runs ~5 weeks.
- **Format**: 48 teams, 104 matches (new expanded format).
- **User's bet shape**: 15-17 leg accumulators targeting x2000+ payout. Per-leg average odds ~1.55 → favorite-heavy picks. Acknowledged as "ridiculous but fun."
- **Bookmaker**: user already has funds at 1xbet. **No public API for placing bets.** Read-only odds scraping is possible but fragile (Cloudflare).
- **Stakes**: real money, small fixed bankroll. Paper-trade group stage to verify calibration; switch to real money for knockouts if model passes.
- **Quality bar**: "serious quality" — full tests + types + lint + CI + schema validation.

---

## Key Questions Explored

- How does terminal-Claude plug into the model — feature extractor, adjustment layer, or Bayesian prior shifter?
- What's success — calibration, ROI, explainability, or learning?
- Which markets to predict? (1X2, O/U goals, corners, BTTS)
- Tournament scope (single league vs WC-only)?
- Stack (Python end-to-end vs split Python/TS)?
- How to trigger per-match Claude scrapes?
- Backtesting approach given tiny tournament sample?
- Real money vs simulated?
- Bookmaker odds source — single book vs line shopping?
- Staking strategy?
- Phase plan given 8-day pre-tournament window?
- Dev rigor floor?

---

## Decisions Made

| Topic | Decision |
|---|---|
| Claude role | **Bayesian prior shifter**: separate qualitative prediction, combined with model output via weighted blend. Both layers explicit, backtestable. |
| Primary goal | **Beat the bookmaker** (positive ROI). Hardest path, accepted. |
| Markets | All four: **1X2, O/U goals, corners, BTTS** — composed into 15-17 leg accumulators targeting x2000+. |
| Scope | **WC 2026 only.** |
| Stack | **Python (model + scraping) + TypeScript UI.** |
| Claude trigger | **Manual.** Claude writes conclusions to a file; UI reads + displays alongside scraped stats and model output; final combined decision shown. |
| Backtest | **Walk-forward across all available majors** (Euro 2024, WC 2022, prior WCs and Euros). |
| Money | **Real money, small fixed stake.** Paper-trade group stage first. |
| Odds source | **Read-only multi-book scrape (1xbet + 2-3 others) for line-shopping intelligence.** Place bets manually at 1xbet. |
| Phasing | **Staged**: Phase 0 (now → June 11) — base model + odds + minimal UI; Phase 1 (group stage) — paper-trade, calibrate, build accumulator builder; Phase 2 (knockouts) — real money, refined system, same-game correlated parlays. |
| Staking | **All three modes available** in UI: fractional Kelly, fixed stake, single-lottery-ticket. Each mode shows P(profit), EV, max drawdown for comparison. |
| Quality | **Full rigor**: pytest + mypy + ruff (Python), strict TS + vitest (UI), Pydantic schemas, CI. |

---

## Architecture (sketch)

```
┌─────────────────────────────────────────────────────────┐
│ Base model: Dixon-Coles / hierarchical Poisson on club  │
│ matches involving WC squad players (last ~3yr).         │
│ Outputs: home_xG, away_xG, shot/corner rate priors.     │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│ Tournament adjuster:                                    │
│ - international form delta                              │
│ - rest/travel asymmetry                                 │
│ - stakes (must-win group stage, knockout pressure)      │
│ - referee tendencies (cards/corners)                    │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│ Per-match Claude scrape (24-48h pre-kickoff):           │
│ injuries, lineups, weather, presser sentiment.          │
│ Outputs structured deltas + narrative reasoning to file.│
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│ Bayesian combiner → posterior joint distribution per    │
│ match over (goals, corners, cards, BTTS).               │
│ Joint, not marginal, so we can price correlated legs.   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│ Accumulator builder:                                    │
│ - reads scraped multi-book odds                         │
│ - finds N-leg combos hitting odds target                │
│ - ranks by EV or P(payout > target)                     │
│ - prefers same-game correlated legs (book mispricing)   │
│ - emits suggestions with stake size per mode            │
└─────────────────────────────────────────────────────────┘
```

### UI shape (per-match view)

```
┌────────────────────────────────────────────────────────┐
│  Match: Spain vs Germany  │  Kickoff: 2026-06-29 21:00 │
├──────────────────┬──────────────────┬──────────────────┤
│ STATS (scraped)  │ MODEL OUTPUT     │ CLAUDE NOTES     │
│ - Last 5 form    │ 1X2: 42/28/30    │ "Musiala out,    │
│ - xG / xGA       │ O2.5: 61%        │  shifts attack   │
│ - H2H            │ Corners μ=10.4   │  via flank.      │
│ - Squad value    │ BTTS yes: 58%    │  Spain pressing  │
│ - Recent intl    │ EV per leg vs    │  high → high     │
│                  │ best book odds   │  corners likely" │
├──────────────────┴──────────────────┴──────────────────┤
│ COMBINED POSTERIOR + RECOMMENDED LEGS for this match   │
│ ☑ Over 2.5 goals @ 1.72  (model 61%, implied 58%, +3%) │
│ ☑ Spain corners over 5.5 @ 1.90  (model 56%, +6%)      │
│ ☐ Spain win @ 1.95  (model 51%, implied 51%, ~0%)      │
└────────────────────────────────────────────────────────┘
```

---

## Key Insights & Non-Obvious Points

1. **Corners are easier than 1X2.** Higher-N event (10-12 per match), variance much lower, law of large numbers cooperates. Books are also generally less efficient on corner markets than on outcome. **This is a real edge axis.**

2. **WC sample is tiny.** ~6 prior tournaments × 64 matches ≈ 400. Cannot train a useful model on WC data alone. Must train base model on **club football involving WC squad players**, then apply a tournament-context layer.

3. **Same-game correlation is the accumulator edge.** Books often price multi-market parlays as if independent — they aren't. Home win + over 2.5 + BTTS-yes in the same match are positively correlated. Modeling the joint distribution (not just marginals) allows finding mispriced same-game accumulators.

4. **Line shopping > model quality at the margin.** Picking the best of 5 books often beats a 1% model improvement. Even though user places at 1xbet only, comparing prices flags when 1xbet's price kills EV → skip that leg.

5. **The "ridiculous x2000 accumulator" framing is not insane if EV is positive per leg.** What kills accumulators is compound book margin. If each leg has, say, +2% EV vs fair price, a 15-leg parlay has ~35% compound EV vs the implied probability. Most accumulator bettors lose because they pick *random* legs with –5% EV each.

6. **Paper-trading group stage is non-negotiable.** Brier score on first 48 matches tells you whether the model is calibrated. Switching to real money before that is gambler-brain, not engineer-brain.

7. **1xbet automation is off the table.** No API. Scraping odds = fragile and a TOS risk; placing bets = account suspension risk. Manual placement only.

---

## Phase Plan

### Phase 0 — Pre-tournament (2026-06-03 → 2026-06-10)

- Project scaffold: Python backend + TS frontend, monorepo or two folders.
- CI: pytest/mypy/ruff/vitest/eslint.
- Data ingestion: FBref / understat / StatsBomb open data for club football.
- Base model: Dixon-Coles + Poisson over goals/corners.
- Walk-forward backtest on Euro 2024 + WC 2022 + prior majors.
- Odds scraper MVP: the-odds-api + 1xbet read-only.
- Minimal UI: fixtures list, per-match 3-panel view, Claude notes file ingest.
- **Out of scope**: accumulator builder. Phase 1 work.

### Phase 1 — Group stage (2026-06-11 → ~2026-06-27)

- Paper-trade matchdays 1-2 → measure Brier / log-loss.
- Build accumulator builder + staking modes UI.
- If calibration good → start small real-money bets matchday 3 onward.
- Iterate Claude-scrape templates per match.

### Phase 2 — Knockouts (~2026-06-27 → tournament end)

- Refined system, larger fractional Kelly stakes.
- Focus same-game correlated parlay opportunities.
- Post-mortem after each round.

---

## Open Questions / Risks

- **Cloudflare on 1xbet**: may require home IP or residential proxy. Test early.
- **Calibration may fail.** If Brier score in group stage matches no-skill baseline, do not switch to real money. Need an honest exit criterion written down.
- **Claude-scrape reproducibility**: my qualitative judgments will vary across runs. Need a structured output schema so the combiner doesn't choke on free-form text.
- **Joint-distribution model complexity**: simpler to start with marginals + a copula or empirical correlation matrix, not a fully joint Poisson. Decision deferred to implementation.
- **Squad selection lag**: final WC squads aren't announced until ~7 days pre-tournament. Player-feature freshness matters.

---

## Action Items

- [ ] Initialize repo structure (Python `apps/predictor`, TS `apps/ui`, shared `packages/schemas`)
- [ ] Set up CI: pytest+mypy+ruff, TS strict + vitest + eslint
- [ ] Identify and verify data sources (FBref, understat, StatsBomb open data, the-odds-api)
- [ ] Test 1xbet odds page scrape from local IP — does Cloudflare block?
- [ ] Define Claude-scrape output JSON schema (Pydantic + Zod)
- [ ] Build Dixon-Coles baseline + walk-forward backtest
- [ ] Define exit criterion: minimum Brier score improvement vs implied-odds baseline required to switch to real money
- [ ] Decide bankroll size and per-mode default fractions

---

## Next Step

Likely path: **`/nf`** to flesh out the predictor as a formal feature spec, then **`/ct`** for the Phase 0 task decomposition. Given timeline pressure (8 days), it may be appropriate to skip straight to `/ct` for Phase 0 and treat the spec as living.
