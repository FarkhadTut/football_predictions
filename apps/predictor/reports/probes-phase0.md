# Phase 0 Data-Source Probes — Findings

**Captured**: 2026-06-03
**Probes**: `the_odds_api_probe.py`, `soccerdata_probe.py`
**Raw outputs**: `reports/probes-the-odds-api.json`, `reports/probes-soccerdata.json`

---

## 1. the-odds-api (Step 0.1 — odds source MVP)

Sport key `soccer_fifa_world_cup` is live and exposed. 72 upcoming fixtures
returned for both `h2h` and `totals`.

| Market | Status | Coverage | Notes |
|---|---|---|---|
| `h2h` (1X2) | ✅ supported | 72 fixtures × 36 books for first fixture | Use this as the primary 1X2 line shop. |
| `totals` (O/U goals) | ✅ supported | 72 fixtures × 16 books | Good for O/U 2.5 calibration market. |
| `btts` | ❌ rejected | `INVALID_MARKET` on `/odds` for this sport | Not exposed for `soccer_fifa_world_cup`. |
| `alternate_totals_corners` | ❌ rejected | `INVALID_MARKET` | Corners are not exposed via the-odds-api for this sport. |

**Sample payloads** captured to `tests/fixtures/odds_api/{h2h,totals}.json` (3 fixtures each) for `respx` mocks.

**Quota**: 4 calls burned (1 `/sports` + 3 `/odds`). Remaining: 496 / 500 monthly. Each probe call costs 1–2 units. Budget the rest carefully.

### Escalations (R2 from the plan)

The decomposition told us to escalate before writing the corners code path if `corners_total` is missing. It is missing. Decisions to confirm with the user **before** Step 5 / Step 6:

1. **BTTS odds**: not on the-odds-api for WC. Options:
   - Drop BTTS from Phase 0 odds layer; model still emits the marginal for backtest calibration; no EV computation for BTTS in Phase 0.
   - Pull BTTS from a secondary book (Pinnacle / 1xbet HTML once that scraper lands).
2. **Corners odds**: same story. Options:
   - Drop corners EV from Phase 0; model still emits `corners_total_9_5` marginal for backtest calibration.
   - Defer corners odds to the 1xbet scraper (Step 5.2) and accept it’s single-book.
3. **Backtest impact**: Brier acceptance gate (REQ-007) compares model probabilities to fair probabilities. Historical fair odds for BTTS/corners come from Football-Data.co.uk’s Pinnacle closing odds (still available for Euro 2024, WC 2022, prior majors), **not** the-odds-api. So the calibration gate is unaffected — only live Phase 0 EV display loses two markets.

**Recommendation**: keep BTTS and corners marginals in the model + UI (model output panel shows them with an "indicative — no book" badge), drop EV computation for those two markets in Phase 0, revisit in Phase 1 when 1xbet HTML scraper matures.

---

## 2. soccerdata / FBref (Step 0.1 — historical data source)

FBref is reachable from this network. Cloudflare did not block the request. Schedule pull for Euro 2024 succeeded in 74.5s with 51 rows × 14 columns.

| Probe | Status | Findings |
|---|---|---|
| `fbref-euro-2024-schedule` | ✅ ok | 51 matches; columns: `round, week, day, date, time, home_team, score, away_team, attendance, venue, referee, match_report, notes, game_id`. No corner column (schedule view has none — expected). |
| `fbref-epl-2023-24-team-stats` | ❌ ValueError | My probe passed `stat_type="passing_types"`; the soccerdata 1.9.0 API only accepts `['schedule', 'shooting', 'keeper', 'misc']` for `read_team_match_stats`. **Corner stats live under `stat_type="misc"`** — recorded for the ingest step. |

### Key facts for the ingest step

- **No Cloudflare block** from this network → the FBref ingest path planned for Step 3 can run unmodified. Mitigation R3 (statsbombpy fallback) stays a fallback, not the primary path.
- **Latency**: ~75s per league-season is slow but tolerable for a one-shot ingest; cache `soccerdata` output per `(league, season)` aggressively.
- **Corner data location**: `FBref(...).read_team_match_stats(stat_type="misc")` — wire this into Step 3.1 ingest.
- **Schedule shape**: `home_team, away_team, date, score, referee, game_id` map cleanly to the planned `matches` table.

---

## 3. Verdict

**No blockers found.** Step 1 (scaffold) can proceed. Step 5 (odds scraper) needs the BTTS/corners scope confirmation listed in §1.

**Single follow-up flagged**: user decision on BTTS/corners EV in Phase 0 before Step 5.1.
