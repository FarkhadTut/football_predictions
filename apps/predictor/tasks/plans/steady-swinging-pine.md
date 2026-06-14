# Plan: OddsPortal historical-odds loader (browser-render + offline cache)

## Context

Phase 0's backtest gate (REQ-007) compares model Brier to an **implied-odds baseline**.
The DB has 283 played matches (goals) across 5 tournaments but **`odds_snapshots` is empty**,
so the backtest reports `nan` baselines and cannot pass/fail.

The originally-assumed source (Football-Data.co.uk) was verified to carry **domestic leagues
only** — no WC/Euro odds. Public datasets are results-only or 1X2-only with unverified coverage.
The one viable source for historical WC/Euro 1X2 + O/U + BTTS odds is **OddsPortal**.

OddsPortal is **reachable from this IP (HTTP 200, no Cloudflare 403)** — unlike FBref/1xbet —
but it **AES-encrypts its AJAX odds payloads** (verified: base64 → 249 bytes ciphertext, not
gzip/zlib/JSON). Plain-HTTP scraping would require extracting+rotating their JS key — brittle.

**Chosen approach (user-approved): browser-render + offline cache.** A headless SeleniumBase
undetected-Chrome (already installed, v4.49.5) loads each page; the app decrypts in-JS and
renders odds into the DOM; we parse the DOM and cache the rendered HTML to disk. Re-runs read
cache offline; only uncached pages re-render. **Verified working**: rendering the WC 2018
results page yielded a 595 KB DOM with 145 decimal-odds hits + team names.

Outcome: populate `odds_snapshots` for the 5 tournaments so `backtest/dataset.py` produces a
real implied-odds baseline and the REQ-007 gate yields genuine pass/fail numbers on
**1x2 / ou_2_5 / btts** (corners remains unavailable — see Scope).

## Scope & market reality

- Backtestable markets from cached data: **1x2, ou_2_5, btts** (3 of 4; corners has no source).
- Tournaments: **5 of 6** — Euro 2016/2020/2024, WC 2014/2018. WC 2022 not loaded (no FBref cache).
- REQ-007 gate (`MIN_MARKETS_PASSING=3`) therefore requires **all 3 available markets** to beat
  baseline. This is a scope deviation from "4 markets × 6 tournaments" — to be recorded in the
  tech-decomposition and `reports/probes-phase0.md` (the FD.co.uk claim there is falsified).

## Target: exact `odds_snapshots` mapping (must match consumers unchanged)

Per `backtest/dataset.py:59` `_MARKET_SPEC` and `_baseline_probs` (lines 246-275), each row needs:

| field | value |
|---|---|
| `match_id` | resolved via **fuzzy** match (see below) |
| `book` | `"oddsportal"` (de-vig averages across books; single book is fine) |
| `market` | `"h2h"` \| `"totals_2.5"` \| `"btts"` |
| `outcome` | `home`/`draw`/`away` \| `over`/`under` \| `yes`/`no` |
| `decimal_odds` | float > 1.0 |
| `fetched_at` | UTC datetime (use a fixed ingest timestamp passed in, not `Date.now`) |

Dedup on natural key `(match_id, book, market, outcome, fetched_at)` — mirror
`the_odds_api.persist_snapshots` (`odds/the_odds_api.py:264`).

## Design

### New files
- `src/predictor/odds/oddsportal/__init__.py`
- `src/predictor/odds/oddsportal/render.py` — `RenderedCache`: given a URL + cache path, return
  cached HTML if present; else render via `seleniumbase.Driver(uc=True, headless=True)`, save to
  disk, return it. Cache dir from new Settings field `oddsportal_cache_dir`
  (`data/oddsportal_cache/`). An `offline: bool` flag makes a cache miss raise (no render) —
  mirrors `fbref_source.enable_offline_cache()` semantics so backtests never hit the network.
- `src/predictor/odds/oddsportal/parse.py` — pure DOM→data functions (no I/O), unit-testable
  against saved fixtures:
  - `parse_results_list(html) -> list[ParsedMatch]` (date, home, away, score, detail-url, 1X2 avg odds)
  - `parse_over_under(html) -> dict[point, (over,under)]` (select the 2.5 line)
  - `parse_btts(html) -> (yes,no) | None`
- `src/predictor/odds/oddsportal/source.py` — `OddsPortalSource`: ties render+parse together.
  `fetch_tournament_odds(name, season) -> list[OddsRow]`. Holds the per-tournament URL +
  encoded-id map (e.g. WC2018 → `/football/world/world-cup-2018/results/`).
- `src/predictor/odds/oddsportal/contracts.py` — `OddsRow` dataclass (frozen):
  `competition, season, home_team, away_team, kickoff_date, market, outcome, decimal_odds`.
  (Date-only key, no exact timestamp — see matching.)
- `src/predictor/odds/oddsportal/load.py` — `load_oddsportal(session, source, name, season,
  *, fetched_at) -> OddsLoadResult`. Resolves each `OddsRow` to a `match_id`, writes
  `OddsSnapshot` rows idempotently. Reuses `OddsSnapshot` model + dedup pattern.
- `scripts/ingest_oddsportal.py` — driver looping the 5 tournaments (mirror
  `scripts/ingest_tournaments.py`), `offline=False` for the populate pass.
- `src/predictor/odds/oddsportal/teammap.py` — alias map normalizing OddsPortal names to the
  FBref names already in `teams` (e.g. `Iran→IR Iran`, `South Korea→Korea Republic`,
  `Turkey→Türkiye`, `USA→United States`, `North Macedonia→N. Macedonia`,
  `Ivory Coast→Côte d'Ivoire`).

### Fuzzy match resolution (new — do NOT reuse exact `_resolve_match`)
`load.py._resolve_match_fuzzy(session, competition, season, home, away, kickoff_date)`:
1. Normalize `home`/`away` through `teammap`.
2. Look up `Team.id` by normalized name.
3. Find the `Match` with that `(home_team_id, away_team_id)` whose `kickoff_utc` date ==
   `kickoff_date` and `competition`/`season` match. (Same calendar day, not exact timestamp.)
4. On no/ambiguous match: log a structured warning, increment `unmatched`, skip — never crash.
Report unmatched count at the end so coverage gaps are visible (quality gate).

### Settings
Add to `config.Settings` (`src/predictor/config.py:18`):
`oddsportal_cache_dir: Path = APP_ROOT / "data" / "oddsportal_cache"`.

### Migration
**None.** `odds_snapshots` already exists and is empty.

## Implementation milestones (incremental — validate cheap before the expensive render pass)

- **M1 — pipeline end-to-end on 1X2 only.** render.py + parse_results_list + teammap +
  fuzzy resolver + load.py + Settings. Populate 1X2 (`h2h`) for all 5 tournaments from the
  results *list* pages (~tens of renders). Run backtest → confirm real 1x2 baseline numbers and
  that fuzzy matching covers ~all 283 matches (check `unmatched`). This de-risks matching before
  ~560 detail renders.
- **M2 — add ou_2_5 + btts.** parse_over_under + parse_btts; source.py renders each match's
  detail sub-tabs (cached). Populate `totals_2.5` + `btts`. Re-run backtest → full 3-market gate.

## Tests (offline, against saved fixtures — mirror `tests/ingest/` FakeSource pattern)
- Save real rendered HTML fixtures under `tests/fixtures/oddsportal/` (one results page, one
  over/under detail, one btts detail).
- `tests/odds/oddsportal/test_parse.py` — parse_* against fixtures: row counts, 1X2 triple sums
  sane, O/U picks the 2.5 line, BTTS yes/no present.
- `tests/odds/oddsportal/test_teammap.py` — every alias resolves to an existing `teams.name`.
- `tests/odds/oddsportal/test_load.py` — `FakeOddsPortalSource` of `OddsRow`s + a seeded match →
  asserts `odds_snapshots` rows, fuzzy match by date, idempotent re-run, unmatched skip path.
- `RenderedCache` offline-miss raises; offline-hit returns cached bytes without a Driver.

## Verification (end-to-end)
1. `uv run python scripts/ingest_oddsportal.py` → logs per-tournament odds rows + unmatched count.
2. `uv run python -c "..."` count `odds_snapshots` grouped by market → h2h/totals_2.5/btts > 0.
3. `uv run python -m predictor.backtest.run ...` (the wired CLI from `c98341a`) → regenerate
   `reports/backtest-phase0.md`; assert non-`nan` baseline ratios for 1x2/ou_2_5/btts and an
   explicit pass/fail.
4. `uv run pytest tests/odds/oddsportal -q` green.
5. `make ci` (ruff, mypy --strict, pytest) clean.

## Risks
- **DOM selectors brittle.** OddsPortal markup changes; parse.py is isolated + fixture-tested so
  breakage is localized and detectable. Selectors to be finalized against live DOM during M1.
- **Matching coverage.** Alias map may miss a team; M1's `unmatched` count surfaces this before M2.
- **Render time.** ~560 detail renders on first pass (minutes, one-time). Cache makes re-runs
  instant; script is resumable (skips cached pages).
- **ToS.** OddsPortal scraping is personal/research use; single-pass, cached, low-rate.

---

## Gemini Review

_Generated: 2026-06-14 19:51:35_

### Summary
The plan is structurally sound and effectively addresses the missing implied-odds baseline issue by using a headless browser to bypass OddsPortal's JS-encryption. The offline caching strategy is an excellent approach for performance, reproducibility, and isolated testability.

### Issues Found
- **[HIGH] Timezone/Midnight drift in `_resolve_match_fuzzy`**: Headless browsers render OddsPortal in the host system's local timezone unless explicitly overridden. Matches occurring near midnight UTC may fall on an adjacent calendar date in the DOM, causing the strict `date == date` fuzzy match to fail.
- **[MEDIUM] N+1 Query Performance during Match Resolution**: The plan suggests mirroring `the_odds_api.persist_snapshots` for resolution. Executing individual `Team` and `Match` lookups for each of the ~850 extracted `OddsRow`s will cause unnecessary N+1 query overhead during a bulk historical ingestion.
- **[LOW] Missing data handling (`-`)**: OddsPortal displays a dash (`-`) when odds are suspended or unavailable. The plan's strict `decimal_odds: float` definition in `OddsRow` will throw a `ValueError` if the parser does not explicitly handle this.
- **[LOW] Architecture / Project Structure Divergence**: Introducing an entire `oddsportal/` sub-package with 6 separate files (e.g., `contracts.py`, `render.py`) diverges from the established pattern in `src/predictor/odds/`, where sources are typically encapsulated in single modules (e.g., `one_x_bet.py` (15KB), `the_odds_api.py` (11KB)).

### Recommendations
- **Implement a +/- 24h Date Tolerance**: In `_resolve_match_fuzzy`, allow a 1-day difference when comparing `kickoff_date` with `Match.kickoff_utc` to safely absorb timezone boundary shifts. The combination of `(competition, season, home, away)` is sufficient to guarantee match uniqueness for these international tournaments.
- **Pre-fetch Reference Data in `load.py`**: Instead of querying `Team` and `Match` per row, fetch all matches for the target `(competition, season)` upfront. Store them in an in-memory dictionary keyed by `(normalized_home, normalized_away)` to resolve matches instantly without database roundtrips.
- **Graceful Parsing Fallbacks**: Ensure `parse.py` ignores outcomes containing `-`, skipping the creation of an `OddsRow` for that specific outcome to prevent runtime crashes.
- **Consolidate the Implementation**: Flatten the proposed `oddsportal/` directory into a single `src/predictor/odds/oddsportal.py` module to maintain architectural parity with existing integrations, grouping pure functions at the top and the I/O operations at the bottom.
