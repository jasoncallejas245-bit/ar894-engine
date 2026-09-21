# Picks Engine — Project Context

This file is for any AI assistant (or future you) picking up this project cold. Read it before touching code. Last verified accurate: 2026-09-13 (project is 8 days old, started 2026-09-05 — if this file claims otherwise, or if it's been more than a few days since this date, treat everything below as possibly stale and re-verify against the live dashboard/data before acting on it).

## Current direction (as of 2026-09-13) — read this first

**Kalshi only.** Two real-money-eligible strategies, both currently paused (see Current safety state below):
- **Solo moneyline yes/no contracts** (pregame): NFL, NCAAF, MLB, UFC, WNBA, NBA. ATP was dropped 2026-09-09 (too volatile for this strategy). Picks favorites using no-vig fair-probability edges from sportsbook odds vs. Kalshi's own price. Early profit-taking (`check_and_close_profitable_positions`) triggers at `PROFIT_CAPTURE_PCT` (default 0.85) — NOT a flat price-gain percentage, it's the share of MAXIMUM POSSIBLE PROFIT already captured (`(current_bid - entry_price) / (1.0 - entry_price)`), changed 2026-09-13. Positions under `MIN_EARLY_EXIT_STAKE_DOLLARS` ($15) are never early-exited.
- **Real Kalshi combo trading** (NEW 2026-09-12, see `combo_trading.py`): Kalshi's actual multivariate-event (MVE) combo product, confirmed live to work — bundles moneyline legs into one real combo contract via an RFQ (Request For Quote) negotiation. **Fixed at exactly 2 legs** (`COMBO_MIN_LEGS`/`COMBO_MAX_LEGS`, changed 2026-09-13 at the user's request — never more than 2). Off by default (`COMBO_REAL_TRADING_ENABLED=false`); also requires `REAL_TRADING_LEAGUES` non-empty. Combo positions are NEVER early-exited (`is_combo=True` positions are explicitly skipped in `check_and_close_profitable_positions`) — confirmed with the user 2026-09-13 that a combo has a real counterparty (the quoting market maker) and no ongoing liquidity to sell back into, unlike a normal Kalshi market. Whether an early-exit-via-a-new-sell-side-RFQ is possible is an OPEN, UNCONFIRMED question (partial evidence: one live quote test showed only a one-sided ask, no bid) — don't assume either way without testing live. No paper/dry-run mode exists for this strategy yet — it goes straight to real money when enabled, which breaks this project's usual "paper-test before real money" pattern; flagged as a real gap, not yet closed.

**The "parlay" paper-trading feature was removed entirely 2026-09-13** (at the user's request, and because its own premise — "Kalshi has no parlay product" — turned out to be wrong once the real combo product above was discovered). If you see references to `PARLAY_ENABLED`, `maybe_make_parlay_pick`, etc. in old commits, that's why they're gone now. `paper_trading.MIN_COMBINED_PROB` (formerly `PARLAY_MIN_COMBINED_PROB`, kept as a backward-compat alias) is still used by the paused props/combo paper builders.

**Discord notifications are off by default** (`DISCORD_NOTIFICATIONS_ENABLED=false`, changed 2026-09-13) — no longer needed, at the user's request. `send_discord()` still logs errors locally for the dashboard's own error card regardless of this flag; only the actual Discord webhook POST is skipped.

**All paper-trading data was wiped 2026-09-13** (moneyline, parlay, and the orphaned "btc" category — real trading history in `ledger.json`/`bot_realized_pnl.json`/`open_positions.json` was NOT touched) so stats reflect only picks made under the current refined rules (strong-tier, 60% floor, etc.), not the earlier less-refined eras. If you're looking at moneyline win-rate/sample-size stats, they restart from zero as of this date — don't assume history from before this exists.

**PrizePicks-style picks are PAUSED (2026-09-11/12), not removed.** Props (`maybe_make_prop_pick`), passing yards (`maybe_make_passing_yards_picks`), WNBA combined-stat picks (`maybe_make_wnba_combined_picks`), and the paper-only moneyline+prop "combo" ticket type (`maybe_make_combo_pick` — NOT the same thing as `combo_trading.py`'s real Kalshi combos, confusing name collision, be careful) all stopped generating new picks after live-checking the real PrizePicks app against this bot's data: SharpAPI's sportsbook-consensus lines can differ drastically from PrizePicks' actual lines (confirmed example: Dante Moore passing yards — this bot said 169.5, PrizePicks' real line was 249.5, an 80-yard gap). Every consensus_prob this system ever generated for a PrizePicks-style pick is therefore unreliable for real betting. There is no PrizePicks API, so there's currently no way to verify their real lines programmatically. Four enable flags gate this, all default `false`: `PROP_PICKS_ENABLED`, `PASSING_YARDS_ENABLED`, `WNBA_COMBINED_ENABLED`, `COMBO_PICKS_ENABLED` (the paper one). Do not re-enable these without a real way to verify PrizePicks' actual numbers.

**Paper-trading early exit disabled 2026-09-21** (`MONEYLINE_PAPER_EARLY_EXIT_ENABLED` default flipped `true`->`false` in `paper_trading.py`), at the user's request — they saw a paper position cash out early for a small ($3) gain and wanted to see what strategy positions would actually pay out if held to the real game result, not an early-exit estimate. Paper moneyline picks now always hold to resolution. This does NOT touch `worker.py`'s `check_and_close_profitable_positions`, which is real-trading-only (operates on `open_positions.json`) and stays as-is since it's inert while `REAL_TRADING_LEAGUES` is empty.

A BTC 15-minute momentum strategy used to run alongside this. Removed entirely 2026-09-10.

Repo: `github.com/jasoncallejas245-bit/ar894-engine`, branch `main`. Local clone lives at `~/ar894-engine` on the owner's Mac — that is the ONLY clone that matters.

## Current safety state (check this first — it changes)

- `REAL_TRADING_LEAGUES` in `worker.py` is currently `set()` — **all real-money sports trading is paused**, for every league, including combo trading (which additionally requires its own `COMBO_REAL_TRADING_ENABLED` flag).
- Everything runs 100% in paper mode right now for moneyline. Do not silently re-enable real trading for any league or the combo module — that decision belongs to the account owner alone.
- Strong-tier bar for real trading is 60% probability + a genuine detected edge (see "Strong/thin tier system" below) — this was raised from 55% on 2026-09-11.

## Strong/thin tier system (2026-09-11)

Every moneyline pick gets a `bet_tier`: `"strong"` (clears the real-trading bar: `MIN_EDGE_PCT` + fee-adjusted margin + `market_probability >= get_favorite_min_prob()`) or `"thin"` (real positive edge, just under that bar). The old third tier, `"skip"` (zero/negative edge, kept purely to build data volume), was removed 2026-09-11 — those picks are no longer generated or stored at all now, not just hidden.

**The dashboard only shows strong-tier picks** (pending picks list, the full moneyline log, too-close picks, and — while they were active — passing yards/WNBA lists). Thin-tier moneyline picks are still generated in the background (`AGGRESSIVE_PAPER_DATA_COLLECTION`, on by default, feeds the adaptive learning system which needs real sample size) but are filtered out of every dashboard view. If you're investigating "why does the dashboard show fewer picks than the data has," this is why — check `bet_tier` before assuming something's broken.

**IMPORTANT gotcha already hit once**: `worker.py` has its own `FAVORITE_MIN_PROB` constant that looks like the real probability floor but is DEAD CODE — `get_favorite_min_prob()` actually delegates to `paper_trading.get_effective_favorite_min_prob()`, which reads `paper_trading.MONEYLINE_FAVORITE_MIN_PROB_DEFAULT`. If you're changing the real probability floor, change the one in `paper_trading.py`, not `worker.py` — editing the wrong one silently does nothing.

## Kalshi's real combo/MVE product (2026-09-12 discovery)

Confirmed live against the real API (not assumed from docs): Kalshi has a genuine multi-leg combo product (`pykalshi`'s `MveCollection` / `client.communications` RFQ system), separate from — and much more real than — the earlier assumption in this codebase that "Kalshi has no parlay product." Key facts, confirmed live:
- `KXMVECROSSCATEGORY-R` is a real collection that accepts an arbitrary combo of moneyline legs across DIFFERENT games/leagues. Per-league "single game" collections (`KXMVENFLSINGLEGAME`, `KXMVENBASINGLEGAME`) only combine markets WITHIN one game — MLB currently has no such single-game collection at all.
- Creating a combo market (`collection.create_market(...)`) is a real, harmless POST — registers the combo as tradeable, costs nothing, places no order.
- A freshly created combo market has NO live price until an RFQ is submitted (`client.communications.create_rfq(...)`). A live market-maker quote was observed to last only ~10-15 seconds before vanishing — this is NOT a stable, always-on price. `combo_trading.py`'s `try_execute_real_combo` polls fast (every ~2s) for a short window and places a real IOC order the instant a quote clears a minimum-edge bar.
- Kalshi's own consumer app also has a "combo" feature with an always-available fixed multiplier — that's a DIFFERENT, worse-priced mechanism (guaranteed-availability margin baked in). The RFQ path can beat it, but isn't guaranteed to produce a quote at all.

## Two real bugs found and fixed 2026-09-13 (props/passing-yards/WNBA never resolved)

Every prop-style pick (2,701+ props, 288 passing yards, 109 WNBA combined, 237 paper combo — all now paused, but this explains why none of that historical data ever resolved) was stuck permanently "pending" due to two compounding bugs, both fixed and verified live:
1. Event ID assignment (4 places in `paper_trading.py`) used `sharpapi_event_id or espn_lookup(...)`. SharpAPI always provides its own internal-format event_id (e.g. `"nfl_49ers_rams_2026-09-10_b3"`), so the `or` never fell through to the real ESPN lookup — grading functions need a real ESPN numeric ID and never got one.
2. `context_data.is_game_final()` searched `get_scoreboard()`, which is ESPN's "today only" scoreboard — even with a correct event_id, a game from a day earlier could never be found. Fixed to query the per-event summary endpoint directly (works for any date).
**Existing pre-fix records still carry the bad event_id and won't auto-resolve retroactively** — this fix only applies going forward. No backfill has been run (as of 2026-09-13; these pick types are paused anyway, and the data is separately compromised by the PrizePicks line-mismatch issue, so backfilling wasn't judged worth it — revisit if that changes).

## Architecture

- `worker.py` — the trading loop. Runs sports-league real trading (gated by `REAL_TRADING_LEAGUES`), unconditional paper trading, the self-adjustment/"learning" calls, and (new) the real combo trading attempt each cycle.
- `combo_trading.py` — real Kalshi RFQ-based combo trading. Off by default. See above.
- `dashboard.py` — Flask app, runs in a background thread in the same process as the worker. Reads the same JSON state files the worker writes.
- `paper_trading.py` — all paper-trading simulation logic: `PAPER_STAKE_DOLLARS` (default $15, NOT $5 — that was wrong in an earlier version of this doc), notional bankroll tracking (`paper_bankroll.json`), moneyline favorite-picking mirrored from real trading, and the now-paused prop-style generators.
- `ledger.py` — real-money budget tracking: percentage-of-balance staking (`STAKE_PERCENT`), `STAKE_MIN_DOLLARS` (raised $1 -> $15 on 2026-09-11), deposit detection, allocation approval.
- `context_data.py` — free ESPN/NWS contextual data (injuries, weather, box scores for prop grading, game-final status). See the 2026-09-13 bugfix above if touching `is_game_final`/`get_player_boxscore_stat`.
- `live_trading.py` — EXPERIMENTAL, isolated, paper-only in-game moneyline strategy + a separate tie-score Discord alert. Both on by default (`LIVE_TRADING_ENABLED`, `TIE_ALERTS_ENABLED`). Status/future relevance under the Kalshi-only direction above: unresolved as of 2026-09-13, ask the account owner before removing.
- `state_io.py` — shared `atomic_write_json` / `safe_read_json`. **Use these, not raw `open()`/`json.load`/`json.dump`**, for any file both the worker and dashboard might touch.

## The "learning" system — what's real vs. cosmetic

One self-adjustment mechanism exists, genuinely functional and statistically gated:

1. **Moneyline favorite threshold** (`maybe_adjust_moneyline_favorite_threshold`): if picks in the "too close" probability band are losing money, and the loss is statistically significant, raises `moneyline_favorite_min_prob`. Only ever tightens, never loosens. Requires `MIN_SAMPLE_FOR_ADJUSTMENT = 30` — as of 2026-09-13 the real sample sits at 28, two away from this ever firing for the first time.

This uses `_mean_is_significantly_negative(values, z=2.0)` — **do not lower `z` below 2.0 without re-testing against pure noise** (an earlier `z=1.0` version false-positive-switched on a 60-coinflip pure-noise sample).

**Principle behind all of this**: don't let cosmetic/dead learning code stay in place pretending to adapt when it isn't, and don't let statistical noise get mistaken for a real signal.

## Known gotchas / environment quirks

- Kalshi API access goes through `pykalshi` (`KalshiClient`, `Action`, `Side`, `MarketStatus`, and now `TimeInForce`, `MveCollection`, `client.communications` for combos).
- Odds data source is SharpAPI. Its `/api/v1/injuries` endpoint is Enterprise-tier (paid), not wired in — ESPN's free undocumented API covers injuries instead.
- Railway deploys automatically from `main` via a `Procfile`. `RAILWAY_VOLUME_MOUNT_PATH` is where persistent JSON state files live in production.
- `paper_trades.json` on production is ~5.3MB, dominated by the now-paused props/passing-yards/WNBA/paper-combo categories (2,701+3,335 total records, zero ever resolved — see bugfix above). This is a real, measured source of slow-down (parsed in full on every dashboard request and worker cycle) and a cleanup candidate — check with the account owner before pruning it, since it's also useful historical evidence of the bugs above.
- `worker.py` used to call `probe_sharpapi_player_prop_market("mlb")` every cycle — removed 2026-09-13. It was a one-time diagnostic from 2026-09-10 that self-guarded after its first run, so it was dead weight, not live waste, but no reason to keep calling it.

## Conventions

- All persisted JSON state goes through `state_io.py`.
- Paper trading exists to generate data and de-risk strategy validation before any real-money toggle is flipped back on — treat its fidelity as seriously as real trading code.
- Any change to what leagues/strategies get real money (including the new combo module) is a decision for the account owner, not something to infer or default to a certain way.
- The account owner wants real information over confident-sounding guesses — verify claims against live data/logs before stating them as fact, especially anything involving dates, timespans, or "is X actually happening."
