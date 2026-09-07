# AR894 Engine

An autonomous trading bot for Kalshi prediction markets — sports moneylines (NFL, NCAAF, MLB, UFC, ATP) and a short-window BTC momentum strategy — with a live Flask dashboard, notional paper-trading simulation, and a gated self-adjustment ("learning") layer.

**Start here:** [`CLAUDE.md`](./CLAUDE.md) has the full picture — architecture, current safety state (real-money trading is currently paused everywhere), how the learning system works, and known gotchas. Read that before making changes.

## Files

| File | Purpose |
|---|---|
| `worker.py` | Main trading loop: real trading (currently gated off), paper trading, and the self-adjustment calls |
| `dashboard.py` | Flask dashboard, runs alongside the worker in the same process |
| `paper_trading.py` | Paper-trading simulation, bankroll tracking, and the learning logic |
| `ledger.py` | Real-money budget tracking (percentage-of-balance staking, deposit detection) |
| `state_io.py` | Shared atomic JSON read/write, used by every module that persists state |
| `context_data.py` | Free ESPN injury data + National Weather Service forecasts for "too close to call" picks |

## Running

Deployed on Railway via the `Procfile`, auto-deploying from `main`. Requires these env vars (set in Railway, not committed): `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` or `KALSHI_PRIVATE_KEY_CONTENT`, `SHARPAPI_KEY`, `DISCORD_WEBHOOK_BETS`, `DISCORD_WEBHOOK_UPDATES`.

## Deploying a change

This repo is usually pushed to from `~/ar894-engine` on the owner's Mac. After pushing to `main`, Railway auto-deploys.
