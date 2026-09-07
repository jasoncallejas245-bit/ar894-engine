# AR894 Engine — Project Context

This file is for any AI assistant (or future you) picking up this project cold. Read it before touching code.

## What this is

A Python trading bot deployed on Railway that trades Kalshi prediction markets. Two strategies:
- **Sports moneylines**: NFL, NCAAF, MLB, UFC, ATP — picks favorites using no-vig fair-probability edges from sportsbook odds.
- **BTC 15-minute momentum**: short-window crypto direction bets on Kalshi's BTC markets.

Repo: `github.com/jasoncallejas245-bit/ar894-engine`, branch `main`. Local clone lives at `~/ar894-engine` on the owner's Mac — that is the ONLY clone that matters; ignore any other stale clone you find (e.g. an old `~/Desktop/ar894-engine`).

## Current safety state (check this first — it changes)

- `REAL_TRADING_LEAGUES` in `worker.py` is currently `set()` — **all real-money sports trading is paused.**
- `BTC_REAL_TRADING_ENABLED` (env var, default `"false"`) — **BTC real-money trading is paused.**
- Everything runs 100% in paper mode right now, across every league plus BTC, with no cap on paper trade volume (deliberately free-range to maximize logged data).
- This was a deliberate decision after MLB/UFC/ATP were found live-trading real money contrary to original intent (one real UFC loss, ~$3.27, was taken before this was caught). Do not silently re-enable real trading for any league — that decision belongs to the account owner alone.

## Architecture

- `worker.py` — the trading loop. Runs sports-league real trading (gated by `REAL_TRADING_LEAGUES`) and BTC real trading (gated by `BTC_REAL_TRADING_ENABLED`), plus unconditional paper trading for everything. Also runs the self-adjustment/"learning" calls each BTC cycle.
- `dashboard.py` — Flask app, runs in a background thread in the same process as the worker. Reads the same JSON state files the worker writes. Shows real trading status, paper bankrolls, and a "Learning / Adaptive Settings" card.
- `paper_trading.py` — all paper-trading simulation logic: fixed $5-per-pick staking (`PAPER_STAKE_DOLLARS`), notional bankroll tracking (`paper_bankroll.json`), moneyline favorite-picking mirrored from real trading, and the BTC momentum-window learning logic.
- `ledger.py` — real-money budget tracking: percentage-of-balance staking, deposit detection, allocation approval. Only relevant to real trading (currently all paused, but stays live for whenever it's turned back on).
- `state_io.py` — shared `atomic_write_json` / `safe_read_json`. **Use these, not raw `open()`/`json.load`/`json.dump`, for any file both the worker and dashboard might touch.** The worker and dashboard are separate concerns sharing plain JSON files as informal IPC; a torn write from one can crash a read from the other. This was a real bug that got fixed — don't reintroduce raw file I/O.

## The "learning" system — what's real vs. cosmetic

Two self-adjustment mechanisms exist. Both are now genuinely functional and statistically gated — this was NOT always true, and it's worth understanding why, so it doesn't regress:

1. **Moneyline favorite threshold** (`maybe_adjust_moneyline_favorite_threshold`): if picks in the "too close" probability band are losing money, and the loss is statistically significant (not just n small and unlucky), raises `moneyline_favorite_min_prob` so future picks require a bigger edge. Only ever tightens, never loosens. Requires `MIN_SAMPLE_FOR_ADJUSTMENT = 30`.

2. **BTC momentum window** (`maybe_adjust_btc_momentum_window`): tests candidate windows (2, 3, 5 ticks) against the *same* resolved historical outcomes (via a stored `price_snapshot` on each paper pick) and switches only if one candidate is significantly better via a paired-difference test. **This used to be a dead stub** — it existed in the code but never actually changed trading behavior (the live trade logic had a hardcoded window, disconnected from whatever the "learning" concluded). It was rebuilt to actually wire the learned window into both `paper_trading.make_btc_paper_pick` and `worker.process_btc_real_trading`.

Both use `_mean_is_significantly_negative(values, z=2.0)` — a standard-error-based significance check, not a raw "did it lose money" check. **Do not lower `z` below 2.0 without re-testing against pure noise.** An earlier `z=1.0` version was caught, via self-authored synthetic testing, false-positive-switching on a 60-coinflip pure-noise sample. If you change this function, re-run a noise test (many trials of random 50/50 outcomes, confirm no switch triggers) before trusting it.

**Principle behind all of this**: the account owner explicitly said not to let this system "lie to ourselves" — i.e., don't let cosmetic/dead learning code stay in place pretending to adapt when it isn't, and don't let statistical noise get mistaken for a real signal. Any future "learning" feature added here should be held to that same bar: it must demonstrably change behavior, and it must be gated on real significance, not vibes.

## Known gotchas / environment quirks

- **This session's git proxy cannot push to this repo directly** (if you're an AI assistant working from a cloud sandbox). Don't waste time retrying `git push` from a sandbox — hand the user complete files or a patch, and have them commit/push from their own `~/ar894-engine` terminal.
- Kalshi API access goes through `pykalshi` (`KalshiClient`, `Action`, `Side`, `MarketStatus`).
- Odds data source is SharpAPI. Its `/api/v1/injuries` endpoint exists but is Enterprise-tier (paid) — not currently wired in. If free injury data is ever wanted, ESPN's undocumented API (`sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/...`) was confirmed working and free, for injuries specifically (no lineup or weather source has been found free yet).
- Polymarket was investigated as an alternate BTC execution venue: global Polymarket has the right short-window markets but blocks US traders; Polymarket US (the CFTC-regulated, KYC'd one, usable from the US) does not currently have short-window BTC markets — confirmed via live in-app search. Dead end for now; revisit if Polymarket US adds these markets or ships a leveraged perpetuals product.
- Railway deploys automatically from `main` via a `Procfile`. `RAILWAY_VOLUME_MOUNT_PATH` is where persistent JSON state files live in production.
- The `.git` history was once bloated to 120MB by an accidentally-committed `venv/` directory; it was purged via `git filter-branch` + aggressive gc, down to ~256KB, with zero change to actual tracked file content. Don't recommit a `venv/` — check `.gitignore` covers it.

## Conventions

- All persisted JSON state goes through `state_io.py`.
- Paper trading exists to generate data and de-risk strategy validation before any real-money toggle is flipped back on — treat its fidelity (accurate staking, accurate bankroll tracking, mirroring real trading's filters) as seriously as real trading code.
- Any change to what leagues/strategies get real money is a decision for the account owner, not something to infer or default to a certain way.
