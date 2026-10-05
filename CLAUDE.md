# AR894 Engine — quick brief

Read this first. Keep it short (under ~5 KB): it loads into every session. Long backstory lives in `docs/HISTORY.md` — open it only for the specific topic you need.

## Mission
Find real, profitable edges in Kalshi sports moneyline markets. Prove them on paper first; real money only once the data shows a genuine edge. Fully automated, no manual steps.

## Status (update this section every time something changes)
- **Strategy version:** v2 (10/5/2026) — `strategy_versions.CURRENT_VERSION`. v1 = DK/FD average (9/13–10/5).
- **Real money:** PAUSED. `REAL_TRADING_LEAGUES = set()` in `worker.py`; combo trading off (`COMBO_REAL_TRADING_ENABLED=false`). Only the account owner turns real money on.
- **Paper bankrolls (fresh $100 each, 10/5):** `moneyline` (strong picks), `moneyline_thin`, `moneyline_sharp` (Pinnacle-only picks), `moneyline_maker` (limit-order shadow). Old history in `moneyline_v1_archive`.
- **What v2 is testing:** edge cap 7%, 50/50 blend with Kalshi price, Pinnacle signal via The Odds API (free plan, every 6h), limit (maker) orders vs taker, closing-line value.
- **Next decision point:** once each v2 bankroll has ~30 resolved picks, compare them; decide on real money / paid odds data.
- **Discord:** off by default.

## Where things are
- `worker.py` trading loop · `paper_trading.py` paper picks + bankrolls · `dashboard.py` Flask dashboard (`/` = clean full view) · `combo_trading.py` real combos (off) · `sharp_odds.py` Pinnacle · `ledger.py` real-money budget · `context_data.py` ESPN data · `state_io.py` safe JSON I/O · `strategy_versions.py` version log · `engine_notes.py` engine's own notebook.
- Deploys: Railway auto-deploys `main`. State JSON lives on the Railway volume.

## Rules
- Any change to how picks are made → add a new version in `strategy_versions.py` and bump `CURRENT_VERSION`.
- Never turn on real money, raise stakes, or remove safety caps without the owner saying so.
- Use `state_io.py` for any JSON both worker and dashboard touch.
- Verify against live data before stating something as fact.
- After a change, update the Status section above in the same commit.

## Gotchas
- Real probability floor is `paper_trading.MONEYLINE_FAVORITE_MIN_PROB_DEFAULT`; `worker.FAVORITE_MIN_PROB` is dead code.
- Learning step needs 30 resolved samples and z ≥ 2.0 — don't loosen.
- SharpAPI is free tier (DK + FD only, delayed) — likely why our fair prob runs overconfident.
- PrizePicks-style prop picks are paused (lines didn't match the real app). Don't re-enable.
- `paper_trades.json` is large (old paused props); ask the owner before pruning.
