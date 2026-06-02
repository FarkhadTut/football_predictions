# Plan Review - Phase 0 Predictor MVP

**Date**: 2026-06-03 | **Reviewer**: `plan-reviewer`
**Task Directory**: `E:\Dev\football_predictor\tasks\task-2026-06-03-phase0-predictor-mvp\`
**Reviewed Document**: `tech-decomposition-phase0-predictor-mvp.md`
**Status**: ⚠️ NEEDS UPDATES

---

## Inputs Reviewed
- Technical decomposition: `tasks/task-2026-06-03-phase0-predictor-mvp/tech-decomposition-phase0-predictor-mvp.md`
- Brainstorm: `docs/brainstorming/brainstorm-2026-06-03-football-match-predictor.md`
- Templates: `.claude/docs/templates/plan-review-template.md`, `technical-decomposition-template.md`
- Codebase: greenfield (no existing code beyond `.claude/` and `docs/`)

---

## Summary
This plan is unusually concrete and well-structured for a greenfield project: requirements, tests, decisions, and step files line up cleanly with the brainstorm. The core risk is **schedule realism**: an 8-day calendar window for a monorepo scaffold + CI + SQLite + multiple ingestors + DC fitter + de-vig + walk-forward backtest + FastAPI + React + WebSocket file-watcher + Playwright E2E + a hard numeric acceptance gate is aggressive. The biggest issues are (a) the Brier ratio gate is mathematically under-specified, (b) several "small" pieces hide significant work (squad seeding, schema codegen pipeline, WebSocket+watcher), and (c) at least two data-source assumptions deserve a verification probe **before** `/si`, not during Step 5.

---

## Reality Check

| Check | Status | Notes |
|---|---|---|
| Real user or system value | ✅ | Plan ships end-to-end working behavior: real fits, real backtest, real UI hitting real endpoints. Not scaffolding. |
| Functional depth | ✅ | DC fitter, market derivation, de-vig, acceptance gate, file-watch UI loop are all real, not stubbed. |
| End-to-end completeness | ⚠️ | Missing: model-output persistence path from `POST /predict` to the `predictions` table, and the actual seam between the backtest pipeline and the API model serving (are these the same `DixonColesModel` instance shape?). |

---

## Implementation Readiness

| Area | Status | Notes |
|---|---|---|
| Step decomposition | ✅ | Steps are atomic, wave-tagged, and reviewable. |
| Sequencing and dependencies | ⚠️ | Step 8.1 (UI codegen) depends on `packages/schemas/openapi.json` existing — but the export build step (sub-step 7.1) is the only producer, and the schemas package is supposed to be the source of truth (decision #6). The flow Pydantic → OpenAPI → Zod is correct, but the file location (`packages/schemas/openapi.json` vs FastAPI's `/openapi.json`) is ambiguous — pick one. |
| File / module specificity | ✅ | Concrete paths everywhere. |
| Codebase fit and reuse | N/A | Greenfield — nothing to fit to. Decisions are internally consistent. |
| Risks / blockers | ⚠️ | R1–R5 named, but no risk addresses **calendar slip**. The 8-day deadline is itself the dominant risk and has no mitigation strategy beyond "trim 1xbet + E2E." |

---

## Testing Review

| Area | Status | Notes |
|---|---|---|
| Requirement coverage | ✅ | REQ-001 through REQ-013 all have tests or verification commands. |
| Functional validation | ⚠️ | TEST-007 (acceptance gate) tests the *checker against a fixture report*, not the gate against a real backtest run. A passing TEST-007 does not prove the model passes its gate. Add a separate "real backtest run produces a report file" integration test, even if it's slow. |
| Edge cases and failures | ⚠️ | Good on API (404/422). Missing: behavior when `the-odds-api` returns 0 markets for a fixture (baseline can't be computed → does that fixture get excluded from Brier, or fail the run?); behavior when WC squads JSON is partial (Step 3.2 says "tolerates partial data" but no test). |
| Verification commands | ✅ | Concrete, runnable, ordered. |

---

## Findings

### Critical Issues

- [ ] **Brier ratio gate is under-specified.** "model Brier ≤ baseline × 0.98 on ≥3 of 4 markets across the held-out tournaments combined" is ambiguous on three points: (1) Is the comparison per-tournament-then-vote, or pooled across all tournaments first? Decision #7 says "pooled" but Must-Haves and REQ-007 say "across the held-out tournaments" which reads either way. (2) For markets where the baseline is partially missing (e.g., the-odds-api lacks corner lines for older tournaments), does that market still count toward the "of 4" denominator? (3) What is the corners baseline at all — bookmaker totals lines vary per book and per tournament era. **Without a precise definition, TEST-007 cannot be written correctly and `/si` will either guess or stall.** → Required: lock the formula as an executable pseudocode block in REQ-007 before `/si`.
- [ ] **Corners baseline data is the silent failure mode.** Pinnacle closing corner totals are not consistently available on Football-Data.co.uk for older tournaments, and the-odds-api historical corners coverage is thin. If the corners baseline is missing for, say, WC 2018 and Euro 2016, the gate becomes "3 of 4 markets with 1 already disqualified." → Required: probe corner-line availability across the four held-out tournaments **before** `/si` (a 30-minute task) and update REQ-007 with what "baseline missing" means for the gate.

### Major Issues

- [ ] **Scope is over-budget for 8 days at the stated rigor floor.** Realistic critical path (Mon–Tue scaffold+CI+DB → Wed–Thu ingest+DC fitter → Fri de-vig+backtest+gate → Sat API+notes → Sun UI+E2E → Mon buffer) leaves ~1 day for unknowns. soccerdata cache priming alone (Step 3.1) for six tournaments + 3 seasons of club football is a half-day if FBref cooperates and ≥2 days if it doesn't. → Recommend an explicit "cut list" decision: if Day 5 EOD the backtest is not green, what gets dropped? Candidates: corners market, Euro 2016/WC 2014, Playwright E2E (→ replace with a Python integration test for the watcher), WebSocket (→ polling).
- [ ] **`soccerdata` for FBref is not a given.** FBref's Cloudflare posture has tightened in the last 12 months; `soccerdata` works but rate-limits aggressively, and StatsBomb open data does not cover club leagues — only some tournaments. R3's "fall back to statsbombpy + worldfootballR datasets" is plausible for tournaments but **does not solve club-level data for WC 2026 squad players** (Step 3.2). → Required: 30-min probe with `soccerdata` before `/si` to confirm we can pull e.g. La Liga 2023-24 match stats. If it fails, Step 3.2 needs a real fallback (paid API, Understat, or scope cut).
- [ ] **WC 2026 squads as a manual JSON seed is a real piece of work.** 48 teams × 26 players = 1,248 players to name-normalize and link to FBref player IDs. Step 3.2 calls this "static `data/wc2026_squads.json` (manually maintained)" and is ~1 line of plan. In practice this is a 4–6 hour data-engineering task with fuzzy name matching, and final squads land 2026-06-04+. → Required: either move squad-linking into its own sub-step with explicit name-resolution logic + tests, or descope club-match training to "top-5 league players + national team appearance ≥ N in last 24 months" to avoid the seeding bottleneck.
- [ ] **OpenAPI source-of-truth flow has a chicken-and-egg.** Decision #6 says Pydantic → OpenAPI → Zod. Step 7.1 says FastAPI generates OpenAPI from its routes. Step 8.1 says codegen reads `packages/schemas/openapi.json`. But `packages/schemas/` is also called the "source of truth" in Must-Haves. → Required: state explicitly that FastAPI is the OpenAPI emitter, `packages/schemas/` holds shared Pydantic models that FastAPI imports, and `packages/schemas/openapi.json` is a *generated artifact* committed for UI codegen. Otherwise step 8.1 can't start until 7.1 ships, which collapses W4 parallelism.
- [ ] **`POST /matches/{id}/predict` has no specified contract or persistence path.** It "triggers a fresh model run" — synchronous or async? Returns the prediction or a job ID? Does it write to `predictions` table and return the row, or just recompute? Tests TEST-008/009/010 do not cover this endpoint at all. → Required: add a TEST and a concrete contract (recommend: synchronous, returns the new `predictions` rows for all 4 markets, idempotent on `(match_id, model_version, kickoff_window)`).

### Minor Improvements

- [ ] **Playwright E2E in CI is overhead on a tight deadline.** Single smoke test is fine, but consider running it only on a `e2e` job that's `continue-on-error: true` for the first deadline week. The behavioral assertion (file watch → UI within 2s) is more cheaply tested as a Python integration test on the WebSocket directly.
- [ ] **`make smoke-live`** target is mentioned but its scope isn't defined. Specify: which endpoints, expected runtime, where credentials come from.
- [ ] **Decision #5 (custom DC fitter)**: `penaltyblog` and `pyDixonColes` are unmaintained but functional reference impls. Worth at least reading their gradient code before writing your own — gradient bugs are silent.
- [ ] **Time-weighting half-life (Step 4.1)** is exposed as a parameter but no default is stated and no test fixes it. Pick a default (e.g., 18 months) and document the rationale.
- [ ] **`watchfiles` + WebSocket** is over-engineered for a 2s SLA on a single-user app. Polling `/matches/{id}/notes` every 1s from the UI would satisfy REQ-011 with no backend state. Reconsider for Phase 0.
- [ ] **`predictions` table schema**: Decision #13 says one row per `(match, market, model_version)` but a market is multi-outcome (1X2 has 3 probabilities, O/U has 2). Either store JSON, or one row per `(match, market, outcome, model_version)`. Clarify before migration is written.
- [ ] **`alembic` for an 8-day greenfield single-developer SQLite project** is rigor for its own sake. SQLModel + `create_all` is fine for Phase 0; introduce alembic at Phase 1 when schema changes start. Saves ~2 hours of yak-shaving.

### Clarifications Needed

- [ ] **Is Phase 0 a "ships green CI + green backtest gate" task, or "ships a usable WC 2026 UI by 2026-06-10"?** These are different scopes. The plan implies both. If the gate fails on 2026-06-10, does Phase 0 ship anyway with a documented failure, or block?
- [ ] **What's the "implied odds baseline" for matches with no closing odds in our sources?** Plan says "Pinnacle where available, the-odds-api otherwise" — but if neither has corners for WC 2018, what happens?
- [ ] **WC 2026 fixtures source** (decision #10): is the static FIFA-schedule JSON committed to the repo or fetched at runtime? Affects whether the UI works offline / in tests.

---

## Answers to Reviewer-Specific Questions

1. **8-day scope realism**: No, not at the full rigor bar with no cuts. Honest cut list: drop alembic, drop WebSocket (poll instead), drop Playwright (Python WS integration test instead), drop Euro 2016 + WC 2014 from backtest (keep 4 tournaments not 6), drop the 1xbet probe entirely (it's already deferred — remove the code stub). That buys ~2 days back.
2. **Test plan sufficiency**: Strong on unit math, weak on (a) acceptance gate against a real run, (b) `POST /predict` contract, (c) baseline-missing semantics, (d) WC squads seed validation, (e) data ingestion idempotency on conflict (says idempotent in 3.1 but no test).
3. **Step concreteness**: Mostly yes. Weakest are 3.2 (squad seeding hand-wave), 7.1 (no `POST /predict` contract), 8.1 (codegen artifact location ambiguous).
4. **Brier gate well-defined**: No — see Critical #1 and #2. Needs executable pseudocode.
5. **Hidden scope creep**: Tournament adjuster is correctly absent. But `corners` market is borderline scope creep for Phase 0 — it's the easiest market per the brainstorm, but it also requires a separate model, a separate baseline, and may have missing reference data. Consider moving corners to Phase 1 and shipping Phase 0 with 1X2 / O-U 2.5 / BTTS only, gate becoming "≥2 of 3."
6. **Data source verification before `/si`**: Yes — three 30-minute probes worth doing first: (a) `soccerdata` pulls a known club league season, (b) `the-odds-api` has WC 2026 fixtures listed with non-empty markets, (c) corner-totals baseline availability for the 4 held-out tournaments. Without these, the plan is partially speculative.
7. **Implementation Decisions to reconsider**: #2 (alembic — drop for Phase 0), #12 (WebSocket — poll instead), #13 (predictions row granularity — must include `outcome`), #6 (clarify the artifact flow), #7 (lock formula as pseudocode).

---

## Decision

**Verdict**: ⚠️ NEEDS UPDATES
**Rationale**: Plan is unusually well-structured, but two Critical items make TEST-007 unimplementable as written, and five Majors hide enough work to credibly miss the 2026-06-10 deadline at the stated rigor bar. None of the fixes are deep — most are 1–2 paragraphs of clarification or a scope trim.
**Ready for `/si`**: After updates (estimated ~2 hours of plan edits + ~90 minutes of pre-`/si` data-source probes).

---

## Revision Checklist
- [ ] Lock Brier gate as executable pseudocode in REQ-007, including treatment of missing baselines.
- [ ] Run 3 pre-`/si` probes: `soccerdata` club league pull, `the-odds-api` WC 2026 coverage, corners baseline availability across 4 tournaments. Document results in the plan.
- [ ] Either move `corners` market to Phase 1 or document the corners-specific baseline source per tournament.
- [ ] Specify `POST /matches/{id}/predict` contract (sync/async, response shape, idempotency) and add a test.
- [ ] Clarify OpenAPI artifact flow: which file lives where, what generates what, what is committed.
- [ ] Decide and document the squad-seeding approach (full manual JSON vs heuristic filter) with concrete test.
- [ ] Define `predictions` row granularity (include `outcome` column or use JSON).
- [ ] Add a documented "cut list" decision rule: if behind on Day 5, what gets dropped first.
- [ ] Drop alembic and WebSocket from Phase 0 (recommended) or justify keeping them under the 8-day budget.
- [ ] Add a calendar-slip risk to the Risks section with a concrete checkpoint date.
