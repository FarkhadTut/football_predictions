> **For agentic workers:** Use `/si` to implement this task. Follow TDD (RED -> GREEN -> REFACTOR). Each step must have a failing test before production code. Update step checkboxes and test evidence during implementation, then use the completion summary for final verification evidence. See `.claude/skills/si/SKILL.md` for the full workflow.

# Technical Decomposition: Phase 0 — Predictor Core MVP
**Status**: In Progress (started 2026-06-03) | **Created**: 2026-06-03

> **Lifecycle:** `Technical Review` -> `In Progress` -> `Implementation Complete`

## Linked Inputs / Context
- Brainstorm: `docs/brainstorming/brainstorm-2026-06-03-football-match-predictor.md` (authoritative product framing)
- Project memory: `project_overview.md`, `feedback_quality_bar.md`, `project_1xbet.md`
- Deadline driver: FIFA World Cup 2026 kicks off **2026-06-11**. Phase 0 must ship by **2026-06-10**.
- Out of scope (Phase 1+): accumulator builder, staking modes UI, real-money flow, multi-book line shopping, joint-distribution modeling for same-game parlays.

---

## Primary Objective
Ship an end-to-end `data → model → 3-panel UI` pipeline for WC 2026 that produces per-fixture Dixon-Coles marginal probabilities for 1X2 / over-under 2.5 goals / BTTS / total corners, displays them next to scraped stats and a file-ingested Claude qualitative note, and **demonstrates ≥2% Brier-score improvement over the implied-odds baseline on at least 3 of 4 markets across the held-out backtest tournaments** (Euro 2024, WC 2022, Euro 2020, WC 2018, Euro 2016, WC 2014).

---

## Must Haves
Non-negotiable truths when this task is complete:
- [ ] Monorepo at repo root: `apps/predictor/` (Python, `uv`-managed) + `apps/ui/` (Vite + React + TS, `pnpm`-managed) + `packages/schemas/` (shared Pydantic ↔ Zod via OpenAPI codegen).
- [ ] CI workflow runs and passes: `pytest`, `mypy --strict`, `ruff check`, `pnpm typecheck`, `pnpm test`, `eslint`. Hooks on PR to `main`.
- [ ] SQLite at `apps/predictor/data/predictor.db` with schema: `teams`, `players`, `wc_squads`, `matches`, `match_stats`, `odds_snapshots`, `predictions`, `score_distributions`, `model_runs`, `claude_notes`. Schema versioned via `alembic`.
- [ ] Observability + secrets: `pydantic-settings` for config (env-driven, `.env.example` checked in, real `.env` git-ignored), `structlog` JSON logging across all entrypoints (FastAPI, backtest CLI, scrapers).
- [ ] Historical data loaded for: (a) club matches involving players in announced WC 2026 squads, last 3 seasons; (b) prior international tournaments — Euro 2024, WC 2022, Euro 2020, WC 2018, Euro 2016, WC 2014.
- [ ] Dixon-Coles model fits team strengths + low-score rho correction; Poisson submodel produces λ_home / λ_away from which 1X2, O/U 2.5, BTTS, total-corners marginals are derived analytically.
- [ ] Walk-forward backtest script outputs a Markdown report at `apps/predictor/reports/backtest-phase0.md` (with a `backtest-phase0.json` machine-readable sidecar) covering all 6 tournaments — Euro 2024, WC 2022, Euro 2020, WC 2018, Euro 2016, WC 2014 — with per-tournament and pooled Brier scores against an implied-odds baseline (Pinnacle closing via Football-Data.co.uk where available, the-odds-api closing snapshot otherwise; overround removed via Shin / power method).
- [ ] **Pass gate**: model Brier ≤ baseline Brier × 0.98 on ≥3 of {1X2, O/U 2.5, BTTS, corners} across the held-out tournaments combined. Report explicitly states pass/fail.
- [ ] `the-odds-api` integration fetches odds snapshots for WC 2026 fixtures into `odds_snapshots`; scheduled job documented (manual cron / Task Scheduler).
- [ ] 1xbet read-only scraper exists with explicit Cloudflare-detection branch; if blocked from local IP, scraper logs the block, raises a typed exception, and **does not fail the build**. Deferral note recorded in Phase 1 backlog.
- [ ] FastAPI backend exposes `GET /fixtures`, `GET /matches/{id}`, `GET /matches/{id}/notes`, `POST /matches/{id}/predict` (triggers a fresh model run). OpenAPI schema served at `/openapi.json`.
- [ ] React UI: fixtures list page (filter by date / group) → per-match 3-panel view (stats / model output / Claude notes). Claude notes panel reflects `apps/predictor/claude_notes/<match_id>.json` content; UI subscribes via SSE and refreshes within 2s of file change.
- [ ] `claude_notes/<match_id>.json` validated by Pydantic schema (`packages/schemas/`) with a typed discriminated union for `qualitative_deltas` (log-odds shift per market); Zod types auto-generated from OpenAPI for the UI; mismatched schemas cause UI to show a clear error per match, not a blank page.

---

## Test Plan (TDD - Define First)

### Test Strategy
- **Unit (Python)**: pure-function logic — Dixon-Coles likelihood + rho correction, Poisson goal-matrix derivation of market marginals, Brier score, odds-to-fair-probability conversion (overround removal), schema validation. Fast, deterministic.
- **Integration (Python)**: SQLite read/write per repository, FastAPI endpoint contract tests using `httpx.AsyncClient`, end-to-end backtest pipeline on a tiny fixture dataset.
- **Unit (TS)**: pure UI components (3-panel layout, prediction table) with vitest + @testing-library/react.
- **Integration (Python, SSE end-to-end)**: spin up FastAPI app via `httpx.AsyncClient`, subscribe to `/events/notes`, write a fixture `claude_notes/<id>.json`, assert subscriber receives parsed payload within 2s. Replaces a UI-driven Playwright spec to fit the 8-day deadline; UI panel rendering is already covered by vitest component tests.
- **Backtest acceptance**: numeric gate as a pytest assertion against the report — fails CI if pass criterion violated, so the gate is enforced not advisory.
- **No live-data tests in CI**: external HTTP (the-odds-api, FBref) is mocked via `respx`. A separate `make smoke-live` target hits real APIs locally.

### Test Cases to Implement

#### Test Suite 1: Dixon-Coles math
**File**: `apps/predictor/tests/model/test_dixon_coles.py`

- [x] **TEST-001**: Likelihood gradient matches numerical gradient on synthetic data
  - **Covers**: `REQ-004`
  - **Given**: 200 synthetic matches generated from known α, β, γ, ρ parameters
  - **When**: analytical gradient of the DC log-likelihood is computed at a perturbed point
  - **Then**: agrees with finite-difference gradient within 1e-4

- [x] **TEST-002**: Fitting recovers known parameters within 5% on 5000 synthetic matches
  - **Covers**: `REQ-004`
  - **Given**: generative DC simulator seeded with `numpy.random.default_rng(42)`, fixed α, β, γ, ρ across 20 teams
  - **When**: `DixonColesModel.fit(matches, seed=42)` runs to convergence
  - **Then**: each team's recovered attack/defence strength is within 5% of truth (deterministic across runs)

- [x] **TEST-003**: Low-score rho correction shifts 0-0 / 1-0 / 0-1 / 1-1 probabilities the right direction
  - **Covers**: `REQ-004`
  - **Given**: fitted model with ρ = -0.1
  - **When**: scoreline matrix computed
  - **Then**: P(0-0) and P(1-1) drop vs uncorrected Poisson; P(1-0) and P(0-1) rise; sum stays 1.0 within 1e-9

#### Test Suite 2: Market derivation
**File**: `apps/predictor/tests/model/test_markets.py`

- [ ] **TEST-004**: 1X2, O/U 2.5, BTTS marginals derived from a goal matrix sum to 1.0
  - **Covers**: `REQ-005`
  - **Given**: any valid 10x10 scoreline probability matrix
  - **When**: `markets.from_score_matrix(M)` is called
  - **Then**: 1X2 triple sums to 1.0 ± 1e-9, O/U 2.5 pair sums to 1.0 ± 1e-9, BTTS pair sums to 1.0 ± 1e-9

- [ ] **TEST-005**: Corner totals modeled as independent Poisson by team, sum is Poisson with rate = sum of rates
  - **Covers**: `REQ-005`
  - **Given**: team corner-rate priors λ_h = 5.2, λ_a = 4.8
  - **When**: P(total corners ≥ k) computed for k in 8..12
  - **Then**: matches scipy `1 - poisson.cdf(k-1, λ_h + λ_a)` within 1e-12

#### Test Suite 3: Odds → fair probability
**File**: `apps/predictor/tests/odds/test_devig.py`

- [ ] **TEST-006**: Multiplicative de-vig (Shin) recovers original probabilities from priced book
  - **Covers**: `REQ-006`
  - **Given**: true probabilities (0.45, 0.28, 0.27) repriced with 4% overround
  - **When**: `devig.shin(book_odds)` is applied
  - **Then**: recovered probabilities are within 0.5% of truth and sum to 1.0

#### Test Suite 4: Backtest pass gate
**File**: `apps/predictor/tests/backtest/test_acceptance.py`

- [ ] **TEST-007**: Backtest report parses and enforces the pass gate
  - **Covers**: `REQ-007`
  - **Given**: a generated backtest report (JSON sidecar to the Markdown) with `model_brier`, `baseline_brier` per market per tournament
  - **When**: `acceptance.check(report)` is called
  - **Then**: returns `pass` iff the pooled-across-tournaments ratio satisfies the gate defined in REQ-007 pseudocode for ≥3 of 4 markets; otherwise `fail` with the specific market(s) below threshold and their ratios listed

#### Test Suite 8: Calibration / reliability
**File**: `apps/predictor/tests/backtest/test_calibration.py`

- [ ] **TEST-014**: Reliability diagram bins and per-bin frequencies are computed correctly
  - **Covers**: `REQ-007`
  - **Given**: synthetic (predicted_prob, observed_outcome) pairs over 1000 events with a known calibration profile
  - **When**: `calibration.reliability(preds, outcomes, n_bins=10)` is computed
  - **Then**: bin edges are uniform on [0, 1], per-bin empirical frequency matches the synthetic profile within sampling error, and the Expected Calibration Error matches a hand-computed reference within 1e-6. The backtest report embeds this diagram per market.

#### Test Suite 5: API contracts
**File**: `apps/predictor/tests/api/test_endpoints.py`

- [ ] **TEST-008**: `GET /fixtures` returns scheduled WC 2026 matches sorted by kickoff
  - **Covers**: `REQ-008`
  - **Given**: 3 fixtures seeded in SQLite with mixed kickoff times
  - **When**: `GET /fixtures` is called
  - **Then**: response is 200, body is a list of `Fixture` schemas in ascending kickoff order

- [ ] **TEST-009**: `GET /matches/{id}/notes` returns parsed Claude note or 404 cleanly
  - **Covers**: `REQ-009`
  - **Given**: `claude_notes/match-42.json` exists with valid schema; `match-43.json` does not exist
  - **When**: `/matches/42/notes` and `/matches/43/notes` are called
  - **Then**: first returns 200 with parsed structured note; second returns 404 with `{"detail": "no_note"}`

- [ ] **TEST-010**: `GET /matches/{id}/notes` returns 422 for malformed Claude note
  - **Covers**: `REQ-009`
  - **Given**: `claude_notes/match-44.json` exists but missing required field `confidence`
  - **When**: `/matches/44/notes` is called
  - **Then**: response is 422 with Pydantic validation error details

#### Test Suite 6: UI panels
**File**: `apps/ui/tests/components/MatchPage.test.tsx`

- [ ] **TEST-011**: 3-panel layout renders stats, model output, and claude note from props
  - **Covers**: `REQ-010`
  - **Given**: `MatchPage` rendered with fixture stub, model output stub, claude note stub
  - **When**: component mounts
  - **Then**: all three labelled panels are in the DOM with expected text content

- [ ] **TEST-012**: When Claude note is missing, that panel shows "awaiting Claude analysis" placeholder
  - **Covers**: `REQ-010`
  - **Given**: `MatchPage` rendered with claude note = null
  - **When**: component mounts
  - **Then**: stats and model panels populate; claude panel shows the placeholder string

#### Test Suite 7: SSE end-to-end (Python)
**File**: `apps/predictor/tests/api/test_notes_sse.py`

- [ ] **TEST-013**: File-write → SSE notification within 2s
  - **Covers**: `REQ-011`
  - **Given**: FastAPI app running under `httpx.AsyncClient`, `claude_notes/` empty, an SSE subscriber connected to `/events/notes`
  - **When**: a valid `claude_notes/match-42.json` file is written to disk
  - **Then**: subscriber receives one `note.updated` event with `match_id=42` and the parsed payload within 2s wall-clock; schema-invalid writes produce a `note.invalid` event carrying the validation error, never crash the watcher

### Verification Commands
```bash
# from repo root
make ci                                    # runs everything below in order

# Python (apps/predictor)
cd apps/predictor && uv run pytest -q
cd apps/predictor && uv run mypy --strict src
cd apps/predictor && uv run ruff check
cd apps/predictor && uv run python -m predictor.backtest.run --report reports/backtest-phase0.md
cd apps/predictor && uv run pytest tests/backtest/test_acceptance.py -q  # pass gate

# UI (apps/ui)
cd apps/ui && pnpm typecheck
cd apps/ui && pnpm test --run
cd apps/ui && pnpm lint
```

### Coverage Notes
- Dixon-Coles fitting: gradient correctness + parameter recovery on synthetic data (no real-data flakiness).
- Market derivation: marginals consistency + corner-Poisson convolution.
- De-vig: validates the implied-prob baseline used in the pass gate — gate is meaningless without this.
- Acceptance test enforces the numeric Brier gate, so CI fails if the model regresses below the threshold.
- SSE integration test covers the only piece of file-watcher → client wiring not covered by component tests; full UI Playwright spec is deferred to Phase 1 to fit the 8-day deadline.
- Live HTTP (the-odds-api, FBref) is mocked in CI; a separate `make smoke-live` target hits real endpoints locally before each deploy.

---

## Technical Requirements

- [ ] `REQ-001`: Monorepo scaffold (`apps/predictor`, `apps/ui`, `packages/schemas`) with `uv` (Python) and `pnpm` (TS) tooling, root `Makefile` exposing `make ci`, `make dev`, `make smoke-live`.
- [ ] `REQ-002`: CI workflow on push and PR to `main` runs `make ci` and fails the build on any check failure.
- [ ] `REQ-003`: SQLite schema with alembic migrations covering `teams`, `players`, `wc_squads`, `matches`, `match_stats`, `odds_snapshots`, `predictions`, `score_distributions`, `model_runs`, `claude_notes`. Idempotent migration up/down round-trip.
  - `score_distributions(match_id, model_run_id, matrix BLOB)`: 10x10 joint scoreline probability matrix per (match, model run) — required for Phase 1 joint-distribution / same-game-correlation work; populated alongside `predictions` from the same model run.
  - `model_runs(id, model_version, git_sha, training_cutoff_utc, fitter_config_json, created_at)`: provenance row each fit writes; `predictions` and `score_distributions` FK to it. Required so backtests are reproducible and Phase 1 A/B comparisons reference an immutable run.
- [ ] `REQ-004`: Dixon-Coles fitter with rho correction, fitting reproduces known synthetic parameters; each fit persists a `model_runs` row and writes results to `predictions` + `score_distributions` linked to that run.
- [ ] `REQ-005`: Market derivation produces 1X2, O/U 2.5, BTTS, total-corners marginal probabilities per fixture from fitted parameters; marginals consistent (sum to 1.0).
- [ ] `REQ-006`: Bookmaker odds de-vig via Shin (or multiplicative fallback) producing implied-odds baseline probabilities used for backtest comparison.
- [ ] `REQ-007`: Walk-forward backtest report covering **all 6 tournaments** (Euro 2024, WC 2022, Euro 2020, WC 2018, Euro 2016, WC 2014) with per-tournament and pooled Brier scores; pass gate enforced as a test.

  **Executable gate (pseudocode — implemented in `apps/predictor/src/predictor/backtest/acceptance.py`)**:
  ```python
  MARKETS = ("1x2", "ou_2_5", "btts", "corners_total_9_5")
  THRESHOLD = 0.98  # model must be ≥2% better than baseline
  MIN_MARKETS_PASSING = 3

  def check(report: BacktestReport) -> AcceptanceResult:
      ratios = {
          m: report.pooled[m].model_brier / report.pooled[m].baseline_brier
          for m in MARKETS
      }
      passing = {m for m, r in ratios.items() if r <= THRESHOLD}
      return AcceptanceResult(
          passed=len(passing) >= MIN_MARKETS_PASSING,
          ratios=ratios,
          passing_markets=passing,
          failing_markets=set(MARKETS) - passing,
      )
  ```
  Report is written as both `reports/backtest-phase0.md` (human-readable) and `reports/backtest-phase0.json` (machine-readable sidecar) so the acceptance test can parse without scraping Markdown.
- [ ] `REQ-008`: FastAPI backend exposes `GET /fixtures`, `GET /matches/{id}`, `GET /matches/{id}/notes`, `POST /matches/{id}/predict`; OpenAPI schema served at `/openapi.json`.
  - **`POST /matches/{id}/predict` contract**: body `{"model_version": str | null, "force_refit": bool = false}`. Behavior: if `force_refit` is false and a `predictions` row exists for `(match_id, model_version_resolved)` newer than the most recent relevant data ingest, return cached. Otherwise enqueue a fit, return `202 Accepted` with `{"model_run_id": int, "status": "running"}`. Cached path returns `200 OK` with the resolved `MarketMarginals`. Idempotent on the cached path; long-running fits are tracked by `model_run_id`.
- [ ] `REQ-009`: Claude notes pipeline — Pydantic schema in `packages/schemas` with `qualitative_deltas` as a typed discriminated union (`market` field discriminator over `1x2 | ou_2_5 | btts | corners_total`, value is a `log_odds_shift: float` with documented sign convention: positive = shift toward "yes/over/home"), codegen to Zod in `apps/ui`, file-based ingest at `apps/predictor/claude_notes/<match_id>.json`, 404 / 422 / 200 semantics on the notes endpoint.
- [ ] `REQ-010`: React UI with fixtures list + per-match 3-panel layout matching the brainstorm sketch.
- [ ] `REQ-011`: UI updates Claude notes panel within 2s of file write via Server-Sent Events (`/events/notes`). SSE chosen over WebSocket because the channel is one-way (server → client), proxies and dev servers handle it natively, and the Phase 1 accumulator UI does not need bidirectional messaging.
- [ ] `REQ-012`: the-odds-api integration writes odds snapshots into SQLite on demand; cached responses for tests.
- [ ] `REQ-013`: 1xbet scraper attempts read; on Cloudflare block, logs typed exception, writes deferral record to `apps/predictor/reports/scraper-status.md`, does not fail any test.
- [ ] `REQ-014`: Configuration loaded via `pydantic-settings` from environment + `.env`; `.env.example` checked in lists every required key (the-odds-api key, log level, DB path, claude_notes path). Real `.env` is git-ignored. Settings object imported by FastAPI, backtest CLI, and scrapers.
- [ ] `REQ-015`: `structlog` JSON logging configured globally (FastAPI, backtest CLI, scrapers); each log line carries `service`, `model_run_id` (where applicable), `match_id` (where applicable). Log level driven from settings.

---

## Implementation Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | UI architecture | Vite + React + FastAPI backend | Cleanest separation for a data app; SSR not needed; OpenAPI → Zod codegen gives end-to-end types without a TS server. |
| 2 | Data persistence | SQLite with `alembic` migrations | Single-file, queryable, mature tooling. DuckDB tempting but adds learning curve for marginal OLAP wins on a small dataset. |
| 3 | Python package manager | `uv` | Fast, lockfile-correct, single-tool resolver. Industry direction. |
| 4 | TS monorepo tooling | `pnpm` workspaces | Lightweight, fast installs, plays well with two apps + shared schemas. |
| 5 | Dixon-Coles fitting | Custom scipy.optimize-based fitter on top of statsmodels Poisson scaffolding | Reference implementations exist but most are unmaintained; building it ourselves means we own the gradient and can extend with the tournament adjuster layer in Phase 1 cleanly. |
| 6 | Schema sharing | Pydantic source of truth → OpenAPI → `openapi-typescript` codegen → Zod refinement | Single source eliminates drift. Hand-written Zod risks divergence. |
| 7 | Backtest pass threshold | Brier ratio ≤ 0.98 on ≥3 of 4 markets, pooled | Strict enough to demonstrate edge (≥2%), permissive enough that 1 market can fail (likely 1X2 given known difficulty). |
| 8 | Implied-odds baseline source | Pinnacle closing where Football-Data.co.uk has it; the-odds-api closing snapshot for newer tournaments | Pinnacle is sharp standard; mixing is honest but documented per tournament in the report. |
| 9 | 1xbet block handling | Typed exception + status report, no test failure | Phase 0 ships on time per brainstorm; 1xbet deferred to Phase 1 with proxy options. |
| 10 | Live fixtures source for WC 2026 | the-odds-api `fixtures` endpoint primary; static JSON seed from FIFA official schedule as fallback | API may have gaps near kickoff; static seed guarantees match cards exist for UI dev. |
| 11 | Historical data loader | `soccerdata` library (FBref + StatsBomb wrappers) with on-disk cache | Handles Cloudflare and rate-limits for us; avoids reinventing scrapers. |
| 12 | UI live updates | FastAPI background task using `watchfiles` to watch `claude_notes/`, push to subscribers via Server-Sent Events at `/events/notes` | One-way channel; SSE works through proxies and dev servers without extra config; Phase 1 needs no client→server push. |
| 13 | Predictions table | One row per (match, market, `model_run_id`); separate `score_distributions` row per (match, `model_run_id`) holding the 10x10 joint matrix; `model_runs` provenance row written per fit | Marginals + joint stored together but normalised; Phase 1 same-game-correlation work reads the joint matrix directly without re-fitting; `model_runs` makes A/B reproducible and backtests auditable. |
| 14 | Time zone handling | All timestamps stored UTC; UI renders user-local via `date-fns-tz` | Standard. WC matches span many TZs. |
| 15 | UI E2E coverage | Drop Playwright in Phase 0; cover SSE wiring with a Python integration test, UI panel rendering with vitest component tests | 8-day deadline. Component tests + the Python SSE test together cover the same risk surface as a full Playwright spec at a fraction of the setup cost. Playwright returns in Phase 1 alongside the accumulator UI. |
| 16 | Tournaments in backtest | Keep all 6 (Euro 2024, WC 2022, Euro 2020, WC 2018, Euro 2016, WC 2014) — reviewer suggested cutting to 4, user overrode | Aggregate sample is small (~6 × 50-64 matches); cutting tournaments to save ingest time risks an underpowered gate. Older tournaments use Pinnacle closing odds via Football-Data.co.uk so they cost API budget nothing. |
| 17 | 1xbet scraper in Phase 0 | Keep full scraper module (probe + Cloudflare branch + status report) — reviewer suggested deferring to Phase 1, user overrode | User wants the scraper interface stable before WC starts so Phase 1 can iterate on parsing only. Cost is one file + one test pair; risk is contained because failures cannot break CI by design. |
| 18 | Squad seeding | Heuristic loader: pull last 12 months of senior caps per qualifying nation from FBref/transfermarkt via `soccerdata`; treat anyone with ≥3 senior caps or active in top-5 league + national-pool as "likely squad"; refine from announced squads as they drop (2026-06-04 onward) | Manually entering ~1,248 players for 48 squads is not realistic in the timeline. The heuristic is partial-data tolerant and the model gracefully handles missing player rows. |
| 19 | Configuration + observability | `pydantic-settings` + `structlog` JSON logging, `.env.example` checked in | Reviewer flagged absence; both are cheap to add at scaffold time and painful to retrofit. |
| 20 | BTTS + corners live odds (Step 0 escalation) | Source BTTS + corner totals from the 1xbet HTML scraper only; the-odds-api `soccer_fifa_world_cup` rejects both as `INVALID_MARKET` (probe 2026-06-03). h2h + totals continue to use the-odds-api as primary line-shop source. If 1xbet HTML fails Cloudflare, the UI shows BTTS/corners model marginals with an "indicative — no book" badge and the EV column is blank for those rows. | Probe finding from Step 0.1 closed off the the-odds-api path. Backtest calibration is unaffected (uses historical Pinnacle closes via Football-Data.co.uk, not the-odds-api). Couples BTTS/corners EV display to R4 — accepted because it preserves the four-market model output that Phase 1 staking depends on. |

---

## Implementation Steps

> Wave annotations: `W0` runs before scaffold; `W1` can start immediately after W0; `W2` depends on `W1`; `W3` depends on `W2`; `W4` runs anywhere after `W1`.

### Step 0: Data-source probes [W0]
- [x] Sub-step 0.1: [REQ-007, REQ-012] Verify upstream coverage before writing ingest code
  - **Files / modules**: `apps/predictor/scripts/probes/the_odds_api_probe.py`, `apps/predictor/scripts/probes/soccerdata_probe.py`, `apps/predictor/reports/probes-phase0.md`
  - **What changes**:
    - `the_odds_api_probe.py`: list sports + markets currently exposed for `soccer_fifa_world_cup`; record which of {h2h, totals, btts, corners} have prices for upcoming fixtures, capture sample payload to fixture dir for `respx` mocks.
    - `soccerdata_probe.py`: attempt FBref pull for one tournament (Euro 2024) + one club season; record whether Cloudflare blocks, time-to-first-byte, and whether corners are populated.
    - Write `reports/probes-phase0.md` summarising coverage gaps; if `corners_total` is unavailable in the-odds-api for WC 2026 fixtures, escalate before writing the corners-market code path.
  - **Tests**: probes are scripts, not pytest targets — output report is the artifact. Both probe scripts executed 2026-06-03; raw outputs at `reports/probes-the-odds-api.json` and `reports/probes-soccerdata.json`; consolidated summary at `reports/probes-phase0.md`.
  - **Findings**: the-odds-api exposes `h2h` (36 books) and `totals` (16 books) for `soccer_fifa_world_cup`; `btts` and `alternate_totals_corners` rejected as `INVALID_MARKET`. FBref reachable from this network (no Cloudflare block); Euro 2024 schedule pulled 51 rows in 75s; corners live under `stat_type="misc"` per soccerdata 1.9.0.
  - **Escalation (R2)**: BTTS + corners EV cannot be computed in Phase 0 against the-odds-api. Recommended scope adjustment captured in `reports/probes-phase0.md` §3 — awaiting user confirmation before Step 5.1.
  - **Depends on**: nothing (can run before any other step; informs Step 5 and Step 6 risk profile).

### Step 1: Repo scaffold + CI [W1]
- [x] Sub-step 1.1: [REQ-001] Initialize monorepo
  - **Files / modules**: repo root, `apps/predictor/pyproject.toml`, `apps/ui/package.json`, `packages/schemas/`, `pnpm-workspace.yaml`, `Makefile`, `.gitignore`, `.editorconfig`
  - **What changes**:
    - Created `apps/predictor` with `pyproject.toml` (Python 3.12, hatch build, ruff + mypy strict, pytest-asyncio auto). Deps: `fastapi`, `uvicorn[standard]`, `sqlmodel`, `alembic`, `pydantic`, `pydantic-settings`, `structlog`, `sse-starlette`, `scipy`, `numpy`, `pandas`, `soccerdata`, `httpx`, `watchfiles`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`, `respx`, `mypy`, `ruff`.
    - Created `apps/ui` with Vite + React 18 + TS strict (`exactOptionalPropertyTypes`, `noUncheckedIndexedAccess` via strict). Deps: `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`, `zod`. Dev: `vitest`, `@testing-library/react`, `eslint` (flat config) + plugins, `openapi-typescript`.
    - Created `packages/schemas` placeholder (OpenAPI→Zod codegen lands in Step 6).
    - `pnpm-workspace.yaml` covers `apps/ui` + `packages/*`.
    - Root `Makefile` with `ci`, `lint`, `test`, `typecheck`, `dev-{api,ui}`, `smoke-live`, `migrate`, `seed`, `probes`. `SHELL := bash` for POSIX recipes (CI uses GNU Make 4.x on Ubuntu).
    - `scripts/ci.sh` portable fallback for local Windows (GNU Make 3.75 from Cygwin can't honor modern SHELL semantics).
    - `apps/predictor/.env.example` committed (real `.env` gitignored). `predictor/observability.py` configures structlog JSON output; smoke-tested in `tests/test_observability.py`.
    - `.editorconfig` added at repo root.
  - **Tests**: `bash scripts/ci.sh` — ALL GREEN. ruff: clean; ruff format: 6/6; mypy: 4 files, 0 errors; pytest: 3 passed; pnpm lint: clean; pnpm typecheck: clean; pnpm test: 1 passed (App.test.tsx).
  - **Depends on**: none.

- [x] Sub-step 1.2: [REQ-002] GitHub Actions CI
  - **Files / modules**: `.github/workflows/ci.yml`
  - **What changes**:
    - Matrix: ubuntu-latest, Python 3.12, Node 24, pnpm 10.
    - Setup uv with cache keyed on `apps/predictor/uv.lock`; `uv sync --frozen --all-groups`.
    - Setup pnpm + Node with pnpm cache; `pnpm install --frozen-lockfile`.
    - Runs the individual steps (ruff lint, ruff format check, mypy, pytest, `pnpm -r lint`, `pnpm -r typecheck`, `pnpm -r test`) directly rather than `make ci` — keeps the workflow readable and avoids the Windows make divergence.
    - `concurrency` group cancels stale runs on the same branch.
  - **Tests**: workflow validates locally via `bash scripts/ci.sh` (same command set as CI). First push to `main` will validate against GH-hosted Ubuntu.
  - **Depends on**: 1.1.

### Step 2: SQLite schema + migrations [W2]
- [x] Sub-step 2.1: [REQ-003] Models + initial migration
  - **Files / modules**: `apps/predictor/src/predictor/db/{__init__,models,session}.py`, `apps/predictor/alembic.ini`, `apps/predictor/migrations/{env.py,script.py.mako,versions/2026_06_04_925fccbb938d_initial_schema.py}`
  - **What changes**:
    - SQLModel entities: `Team`, `Player`, `WCSquad`, `Match`, `MatchStat`, `OddsSnapshot`, `MarketAvailability` (decision #20), `ModelRun`, `Prediction`, `ScoreDistribution`, `ClaudeNote`.
    - `predictions` uniqueness extended to `(match_id, market, outcome, model_run_id)` — `outcome` is part of the natural key (home/draw/away).
    - `score_distributions.matrix` = `LargeBinary` containing `numpy.save(allow_pickle=False)` of the 10x10 joint matrix.
    - `model_runs.fitter_config_json` stored as `JSON` column (sqlite stores as TEXT).
    - Indexes per plan: `ix_matches_kickoff_utc`, `ix_odds_snapshots_match`, `ix_predictions_match_run`, `ix_score_distributions_match_run`.
    - `db/session.py`: cached engine-per-URL factory; SQLite uses `StaticPool` for in-memory; `reset_engines_for_tests()` for test isolation.
    - Alembic config: `alembic.ini` (blank URL — env reads from `Settings`), `migrations/env.py` imports `predictor.db.models` to register tables, uses `render_as_batch=True` for SQLite-safe DDL, `script.py.mako` pre-injects `import sqlmodel` so autogenerated revs reference `sqlmodel.sql.sqltypes.AutoString` cleanly.
    - Initial revision `925fccbb938d` autogenerated from the metadata.
  - **Tests**: `tests/db/test_migrations.py` — (1) `upgrade head` creates every expected table, then `downgrade base` leaves only `alembic_version`; (2) post-`upgrade head`, one row per entity is written and read back across a fresh session, including a numpy round-trip on `score_distributions.matrix` and JSON dict on `model_runs.fitter_config_json`. `uv run pytest tests/db -v` → 2 passed in 13.46s. Full quality gate (`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`) → ruff clean, format clean (14 files), mypy 10 files 0 errors, pytest 5 passed.
  - **Depends on**: 1.1.

### Step 3: Historical data loaders [W2]
- [x] Sub-step 3.1: [REQ-003] Tournament loader
  - **Files / modules**: `apps/predictor/src/predictor/ingest/tournaments.py`, `apps/predictor/tests/ingest/test_tournaments.py`
  - **What changes**:
    - `load_tournament(session, source, name, season)` upserts `matches` + `match_stats` from a `TournamentSource`. Idempotent on `(competition, season, home_team_id, away_team_id, kickoff_utc)` for matches and `(match_id, team_id)` for stats. Returns `LoadResult` with insert/update counts.
    - `TOURNAMENT_CATALOG` maps friendly names ("Euro 2024", "WC 2022", …) → FBref league ids (`INT-European Championship`, `INT-World Cup`).
    - Source decoupling: caller-defined `TournamentSource` Protocol returns typed `ScheduleRow` / `TeamMatchStatRow` dataclasses. Production FBref adapter (thin `soccerdata.FBref` wrapper) deferred — Protocol contract is enough for unit-testable ingestion; adapter will be added alongside the smoke-live step.
  - **Decision deviation**: Plan originally specified `respx`-mocked `soccerdata` HTTP. `soccerdata` does its own disk caching + rate limiting + multi-stage HTTP, making HTTP-level mocks fragile. Replaced with `FakeSource` injected via Protocol — cleaner contract, faster tests, no transport coupling.
  - **Tests**: `uv run pytest tests/ingest -v` → 7 passed in 27.50s. Covers: catalog lookup (known + unknown), first-load inserts (4 teams, 3 matches, 4 stats — one scheduled + two final), idempotent reload (all counts zero), score-update reload (matches_updated == 1, status → "final"), stat-update reload (stats_updated == 1), orphan stat skipped. Full quality gate: `uv run ruff check .` clean, `uv run ruff format --check .` clean (18 files), `uv run mypy` 14 files 0 errors, `uv run pytest` 12 passed in 32.24s.
  - **Depends on**: 2.1.

- [x] Sub-step 3.2: [REQ-003] Club matches for WC 2026 squad players (heuristic seeding)
  - **Files / modules**: `apps/predictor/src/predictor/ingest/clubs.py`, `apps/predictor/src/predictor/ingest/squad_heuristic.py`, `apps/predictor/src/predictor/ingest/squads.py`, `apps/predictor/src/predictor/ingest/contracts.py`, `apps/predictor/src/predictor/ingest/_upsert.py`, `apps/predictor/data/wc2026_squads.json`
  - **What changes**:
    - `squad_heuristic.candidates_for(source, nation, as_of_date)` returns "likely squad" players based on: ≥3 senior caps in the last 12 months OR ≥1 start in a top-5 league + ≥1 senior cap. Pure function on a `PlayerCapsSource` Protocol (FBref adapter deferred to live-smoke step).
    - `clubs.load_recent_club_matches(session, source, player_fbref_ids, since)` upserts club-match schedule + per-team stats for the candidate pool via a `ClubMatchSource` Protocol. Reuses the shared `_upsert.upsert_schedule_and_stats` helper extracted from the tournament loader so both paths share natural-key idempotency.
    - `squads.write_heuristic_squads` / `write_announced_squads` persist `players` + `wc_squads` rows with `source` ∈ {`heuristic`, `announced`}. Player natural key `(name, nation)`; refreshes `fbref_id`/`position` when later writes carry richer metadata. `load_announced_squads_json` parses the on-disk feed.
    - `data/wc2026_squads.json` ships as an empty scaffold (`nations: {}`); to be populated as federations announce squads from 2026-06-04 onward. Downstream `--source heuristic|announced|merged` view flag deferred until model fitting needs to pick.
  - **Decision deviation**: Replaced "mocked HTTP for one team" with Protocol-based `FakeClubSource` for parity with the Step 3.1 contract pattern. Extracted shared `contracts.py` (`ScheduleRow`, `TeamMatchStatRow`) + `_upsert.py` so club + tournament loaders share the upsert core verbatim.
  - **Tests**: `uv run pytest` → **37 passed in 65.82s**. New coverage: `test_squad_heuristic.py` (15 tests — rule (a), rule (b), top-5 league parametrization, exclusion edge cases, sorting, window enforcement, nation filtering); `test_clubs.py` (2 tests — first-load insert + idempotent reload, candidate pool forwarded to source verbatim); `test_squads.py` (8 tests — heuristic insert, idempotency, metadata refresh, heuristic+announced coexistence for same player, announced idempotency, JSON round-trip incl. missing-optional and empty-nations scaffolds). Full quality gate: `uv run ruff check .` clean, `uv run ruff format --check .` clean, `uv run mypy` 22 source files 0 errors.
  - **Depends on**: 2.1.

### Step 4: Dixon-Coles model + market derivation [W3]
- [x] Sub-step 4.1: [REQ-004] DC fitter
  - **Files / modules**: `apps/predictor/src/predictor/model/dixon_coles.py`, `apps/predictor/tests/model/test_dixon_coles.py`
  - **What changes**:
    - `DixonColesModel` with `.fit(matches: pd.DataFrame)`, `.predict(home, away) -> ScoreMatrix`, `.predict_lambdas`.
    - `DixonColesParams` frozen dataclass (teams + α / δ / γ / ρ) with mean-zero α/δ identifiability post-fit (α_0 held at 0 during opt; γ absorbs the re-centering shift).
    - Time-weighted log-likelihood (`2^(-Δdays / half_life_days)`; `half_life_days=None` → uniform).
    - L-BFGS-B via `scipy.optimize.minimize` with **analytical gradient** (Poisson piece `(x − λ)` per match scattered through `np.add.at` to α/δ; τ-correction partials applied on the four low-score masks).
    - Public `tau_correction(x, y, λ, μ, ρ)` vectorized broadcast helper.
    - `pyproject.toml`: pandas added to mypy `ignore_missing_imports`; ruff `RUF001/RUF002/RUF003` ignored for `model/` files so Greek mathematical notation (α/β/γ/δ/λ/μ/ρ/τ) is permitted in docstrings.
  - **Tests**: TEST-001 ✅, TEST-002 ✅, TEST-003 ✅. `uv run pytest tests/model/test_dixon_coles.py` → 3 passed in 2.64s; full suite `uv run pytest` → 40 passed in 67.74s.
  - **Decision deviation**: TEST-003 spec text had the ρ-correction direction reversed. Canonical Dixon & Coles (1997) convention has τ(0,0)=1−λμρ, τ(1,1)=1−ρ, so for empirically-fit ρ < 0 the (0,0) and (1,1) draw cells **rise** while (1,0) and (0,1) narrow-win cells **fall** — the implementation follows this convention and the test asserts in that direction with a comment cross-referencing the spec deviation.
  - **Depends on**: 1.1.

- [ ] Sub-step 4.2: [REQ-005] Markets from score matrix
  - **Files / modules**: `apps/predictor/src/predictor/model/markets.py`
  - **What changes**:
    - `from_score_matrix(M) -> MarketMarginals` for 1X2, O/U 2.5, BTTS.
    - Separate `corner_total_prob(λ_h, λ_a, threshold)` using Poisson convolution.
  - **Tests**: TEST-004, TEST-005.
  - **Depends on**: 4.1.

### Step 5: Odds ingestion + de-vig baseline [W3]
- [ ] Sub-step 5.1: [REQ-012] the-odds-api client
  - **Files / modules**: `apps/predictor/src/predictor/odds/the_odds_api.py`
  - **What changes**:
    - Typed client for fixtures + odds endpoints.
    - Writes snapshots to `odds_snapshots(match_id, book, market, outcome, decimal_odds, fetched_at)`.
    - Env-driven API key, rate-limit aware.
  - **Tests**: `respx`-mocked responses; idempotent write on duplicate snapshots.
  - **Depends on**: 2.1.

- [ ] Sub-step 5.2: [REQ-006] De-vig
  - **Files / modules**: `apps/predictor/src/predictor/odds/devig.py`
  - **What changes**:
    - `shin(book_odds)` and `multiplicative(book_odds)` returning fair probabilities.
    - Helper to compute implied baseline per market per match from latest snapshot.
  - **Tests**: TEST-006.
  - **Depends on**: 5.1.

- [ ] Sub-step 5.3: [REQ-013] 1xbet read-only scraper with Cloudflare branch
  - **Files / modules**: `apps/predictor/src/predictor/odds/one_x_bet.py`, `apps/predictor/reports/scraper-status.md`
  - **What changes**:
    - Attempt fetch with realistic browser headers via `httpx`.
    - Detect Cloudflare challenge page; raise `CloudflareBlocked`.
    - Parse h2h, totals, **BTTS, and corner totals** (decision #20: 1xbet is the only Phase 0 source for BTTS + corners).
    - On `CloudflareBlocked`, persist a `MarketAvailability(market, reason="cloudflare_blocked")` row so the API layer (Step 7) can return an explicit "indicative — no book" flag for BTTS/corners EV.
    - CLI: `uv run python -m predictor.odds.one_x_bet probe` writes status report.
  - **Tests**: unit test with two recorded HTML samples (200 with full markets, 403 Cloudflare); a third sample missing the BTTS block exercises the partial-coverage path. No live HTTP in CI.
  - **Depends on**: 5.1.

### Step 6: Walk-forward backtest + acceptance gate [W3]
- [ ] Sub-step 6.1: [REQ-007] Backtest harness
  - **Files / modules**: `apps/predictor/src/predictor/backtest/run.py`, `apps/predictor/src/predictor/backtest/metrics.py`
  - **What changes**:
    - For each held-out tournament: fit DC on all data prior to tournament start (writing a `model_runs` row per fit), predict each match, compare against implied-odds baseline derived from de-vigged closing odds.
    - Output `reports/backtest-phase0.md` (human-readable, includes reliability diagrams from TEST-014) **and** `reports/backtest-phase0.json` (machine-readable sidecar consumed by `acceptance.check`).
  - **Tests**: `tests/backtest/test_metrics.py` validates Brier calculator; TEST-007 enforces the acceptance gate against a fixture report.
  - **Depends on**: 4.2, 5.2.

### Step 7: FastAPI backend + claude_notes ingest [W4]
- [ ] Sub-step 7.1: [REQ-008] HTTP endpoints
  - **Files / modules**: `apps/predictor/src/predictor/api/main.py`, `apps/predictor/src/predictor/api/routes/`, `packages/schemas/openapi.json`, `apps/ui/src/api/openapi.json` (symlink or build-step copy)
  - **What changes**:
    - Routes: `GET /fixtures`, `GET /matches/{id}`, `GET /matches/{id}/notes`, `POST /matches/{id}/predict` implementing the REQ-008 contract (cached → 200, enqueued fit → 202 with `model_run_id`).
    - `make schemas` step: run FastAPI in schema-only mode, dump `app.openapi()` to `packages/schemas/openapi.json`, propagate into `apps/ui` for codegen. CI runs `make schemas` and diffs — any uncommitted change to OpenAPI fails the build (catches drift).
  - **Tests**: TEST-008, TEST-009, TEST-010; contract test asserting cached vs enqueued semantics on `POST /predict`.
  - **Depends on**: 2.1, 4.2.

- [ ] Sub-step 7.2: [REQ-009, REQ-011, REQ-014, REQ-015] Claude notes file watcher + SSE stream
  - **Files / modules**: `apps/predictor/src/predictor/api/notes_watcher.py`, `apps/predictor/src/predictor/api/events.py`, `apps/predictor/claude_notes/.gitkeep`, `packages/schemas/src/claude_note.py`
  - **What changes**:
    - Pydantic `ClaudeNote` schema: `match_id: int`, `created_at: datetime`, `summary: str`, `qualitative_deltas: list[QualitativeDelta]`, `confidence: float`, `sources: list[str]`.
    - `QualitativeDelta` is a discriminated union over `market: Literal["1x2","ou_2_5","btts","corners_total"]` carrying `log_odds_shift: float` with documented sign convention (positive = shift toward "yes/over/home").
    - Background task using `watchfiles` to detect changes in `claude_notes/`, emit `note.updated` or `note.invalid` events on the SSE stream at `/events/notes` via `sse-starlette`.
    - All log lines from the watcher use `structlog` with `match_id` bound.
  - **Tests**: TEST-013 (SSE integration), schema validation unit tests for valid + invalid payloads.
  - **Depends on**: 7.1.

### Step 8: React UI [W4]
- [ ] Sub-step 8.1: [REQ-010] Codegen + API client
  - **Files / modules**: `apps/ui/src/api/`, `apps/ui/scripts/codegen.ts`
  - **What changes**:
    - `pnpm codegen` runs `openapi-typescript` against `packages/schemas/openapi.json` → `apps/ui/src/api/types.gen.ts`.
    - Thin fetch client wrapped in `@tanstack/react-query`.
  - **Tests**: type-level — UI fails typecheck if a route signature drifts.
  - **Depends on**: 7.1.

- [ ] Sub-step 8.2: [REQ-010] Fixtures list page
  - **Files / modules**: `apps/ui/src/pages/Fixtures.tsx`
  - **What changes**:
    - List of upcoming fixtures grouped by date.
    - Click → navigate to `/matches/:id`.
  - **Tests**: component test rendering with mocked react-query data.
  - **Depends on**: 8.1.

- [ ] Sub-step 8.3: [REQ-010, REQ-011] Match page (3-panel)
  - **Files / modules**: `apps/ui/src/pages/Match.tsx`, `apps/ui/src/components/panels/{Stats,Model,ClaudeNote}.tsx`, `apps/ui/src/hooks/useNotesStream.ts`
  - **What changes**:
    - Layout matches the brainstorm sketch.
    - `useNotesStream` subscribes to `/events/notes` via `EventSource`, filters on `match_id`, updates the ClaudeNote panel; falls back to a polling refetch every 10s if the SSE connection drops.
    - Empty/loading/error states for each panel.
  - **Tests**: TEST-011, TEST-012; mocked `EventSource` test for the hook.
  - **Depends on**: 8.1, 7.2.

---

## Dependencies / Risks / Blockers

### Dependencies
- External APIs: `the-odds-api` (free tier 500 req/mo — manage with caching), `soccerdata` library for FBref + StatsBomb access.
- Static data: WC 2026 squad rosters must be manually seeded as final squads are announced (~7 days pre-tournament).
- Tooling: `uv`, `pnpm`, Python 3.12, Node 24.

### Risks
- **R1: Calibration fails the 0.98 Brier gate.** WC sample is tiny; club-only training may not transfer to international play. Mitigation: report identifies *which* markets fail; corners and totals likely to pass even if 1X2 doesn't, which still unlocks Phase 1 paper-trade on the markets that do pass.
- **R2: the-odds-api WC 2026 coverage incomplete.** **RESOLVED 2026-06-03 via Step 0.1 probe + decision #20**: `soccer_fifa_world_cup` exposes `h2h` and `totals` only (72 fixtures × 36 / 16 books). `btts` and `alternate_totals_corners` rejected as `INVALID_MARKET`. BTTS + corners EV now sourced exclusively from the 1xbet HTML scraper (Step 5.2 / 5.3). h2h + totals stay on the-odds-api. See `apps/predictor/reports/probes-phase0.md`. New coupled risk filed as R8.
- **R3: FBref Cloudflare blocks `soccerdata`.** **PROBED 2026-06-03**: no block from this network; Euro 2024 schedule pulled in 74.5s. Primary path stays unmodified. Mitigation retained: `soccerdata` has built-in throttling; if blocked later, fall back to local cached snapshots from public archives (statsbombpy + worldfootballR datasets).
- **R4: 8-day deadline.** Mitigation: Playwright E2E dropped in favor of a Python SSE integration test + vitest component coverage (decision #15); UI scope limited to fixtures list + match page. (1xbet scraper is no longer a "soft" risk — see R8.)
- **R6: Heuristic squad list misses late call-ups or includes wrong players.** Mitigation: model handles missing-player rows gracefully; the `--source announced` flag lets us re-run training once final squads drop (~2026-06-04 onward) without code changes.
- **R7: Older tournaments (Euro 2016, WC 2014) have spottier coverage in Football-Data.co.uk.** Mitigation: the per-tournament report flags any matches dropped for missing baseline odds; gate threshold is computed only on matches with both model + baseline probabilities.
- **R5: Pydantic ↔ Zod codegen drift.** Mitigation: codegen runs in CI; any divergence fails typecheck on the UI side.
- **R8: BTTS + corners EV depends entirely on the 1xbet HTML scraper (decision #20).** Cloudflare block on 1xbet now degrades two things at once: the live scraper *and* EV display for BTTS + `corners_total_9_5`. Mitigation: model marginals for both markets are always emitted by the predictor regardless of scraper state; UI shows them with an "indicative — no book" badge and a blank EV column when `MarketAvailability` rows record `reason="cloudflare_blocked"`. Backtest calibration is unaffected (uses Football-Data.co.uk historical Pinnacle closes, not live odds). Phase 1 escalation path: add a second BTTS/corners book (Bet365 HTML or Pinnacle direct) once the staking UI lands.

### Blockers
- None for starting Step 1. Step 3.2 (club matches for squad players) is partially blocked on final squad announcements (~2026-06-04 onward); the loader tolerates partial squads, so this does not block Phase 0 completion.

---

## Tracking / Notes (Optional)

### Tracking
- **Issue ID**: not yet created
- **Issue URL**: n/a
- **Branch / PR**: `main` (single-developer repo; conventional commits per step)
- **Split status**: NO SPLIT — task-splitter evaluated 2026-06-03 and recommended keeping as one task. Reasoning: the Brier acceptance gate cannot be evaluated until data + model + de-vig coexist; UI is contract-coupled to API via OpenAPI codegen; Wave annotations (W0–W4) already give internal parallelism without multi-task coordination overhead.

### Notes
- Phase 1 backlog (do not stealth-add to Phase 0): accumulator builder, staking modes UI, joint distribution / copula for same-game correlation (the `score_distributions` table is groundwork for this), multi-book line shopping, tournament-context adjuster (rest, intl form, stakes), real-money bet recommendation flagging, Playwright E2E spec covering accumulator + match flows, full announced-squad ingest replacing the heuristic.
- The "manual placement only at 1xbet" rule applies forever — never propose betting automation.
- Brier gate is intentionally strict on the *aggregate* but permissive on which 1 market may fail. This protects against overfitting to 1X2 specifically.

---

## Completion Summary (Refresh before review handoff)

**Implementation Complete**: _to be filled during `/si` completion._

### Verification Evidence
- **Commands run**: _to be filled_
- **Quality gate**: _to be filled_
- **Goal verification**: _to be filled_
- **Known skips / caveats**: _to be filled_

**Commits / PRs**: _to be filled_

**Deferred Follow-ups**: _to be filled_
