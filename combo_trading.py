import os
import time
from datetime import datetime

from pykalshi import Action, Side, TimeInForce

import paper_trading as pt
import ledger

# ---------------------------------------------------------------------------
# Real Kalshi MVE combo trading (NEW 2026-09-12, at the user's explicit
# request, built after confirming live on the real API that this works).
#
# This is SEPARATE from paper_trading.py's earlier "combo" ticket type
# (moneyline + PrizePicks-style props, paper-only, now paused). THIS is
# Kalshi's own real multi-leg combo product -- a multivariate event (MVE)
# market you can actually buy real contracts on.
#
# Confirmed live 2026-09-12:
#   - KXMVECROSSCATEGORY-R is a real collection that accepts an arbitrary
#     combo of moneyline legs across DIFFERENT games/leagues (unlike the
#     per-league "single game" collections, e.g. KXMVENFLSINGLEGAME, which
#     only combine markets WITHIN one game).
#   - Creating the combo market (collection.create_market) is a real,
#     harmless POST that just registers the combo as tradeable -- costs
#     nothing, places no order.
#   - A freshly created combo market has NO live price (yes_ask/yes_bid
#     both effectively empty) until an RFQ (Request For Quote) is
#     submitted, broadcasting intent to trade -- market makers can then
#     respond with a real, live quote.
#   - That quote is FLEETING: in live testing it appeared for roughly
#     10-15 seconds before vanishing again. This is why this module polls
#     fast (every couple seconds) for a short window, not on the bot's
#     normal multi-minute scan cadence.
#   - Kalshi's own consumer app also offers a "combo" feature with an
#     always-available fixed multiplier -- that's a DIFFERENT, worse-priced
#     mechanism (it prices in a guaranteed-availability margin). The RFQ
#     path can beat it when a market maker actively wants the flow, but
#     isn't guaranteed to produce a quote at all.
#
# OFF by default (COMBO_REAL_TRADING_ENABLED=false). Turning this on lets
# the bot spend real money placing real IOC orders against live RFQ
# quotes. Read every comment in this file before enabling in production.
# ---------------------------------------------------------------------------

COMBO_REAL_TRADING_ENABLED = os.getenv("COMBO_REAL_TRADING_ENABLED", "false").lower() == "true"

COMBO_COLLECTION_TICKER = os.getenv("COMBO_COLLECTION_TICKER", "KXMVECROSSCATEGORY-R")

COMBO_MIN_LEGS = int(os.getenv("COMBO_MIN_LEGS", "2"))
COMBO_MAX_LEGS = int(os.getenv("COMBO_MAX_LEGS", "3"))
COMBO_STAKE_DOLLARS = float(os.getenv("COMBO_STAKE_DOLLARS", "15.0"))

# How long to watch a live RFQ for a fillable quote before giving up, and
# how often to check. A live quote was observed to last only ~10-15s, so
# this has to poll fast -- the bot's normal scan cadence (60-300s) is far
# too slow to ever catch one.
COMBO_RFQ_WATCH_SECONDS = float(os.getenv("COMBO_RFQ_WATCH_SECONDS", "30"))
COMBO_RFQ_POLL_INTERVAL_SECONDS = float(os.getenv("COMBO_RFQ_POLL_INTERVAL_SECONDS", "2"))

# Required real margin between the combo's true combined probability
# (product of each leg's own sportsbook-consensus probability) and the
# actual quoted price, before this will buy it -- so it only fires on a
# genuinely good, mispriced quote, not just any live quote. E.g. 0.03
# means the quote must be at least 3 percentage points cheaper than what
# the legs are actually worth.
COMBO_MIN_EDGE = float(os.getenv("COMBO_MIN_EDGE", "0.03"))


def _event_ticker_from_market_ticker(market_ticker):
    """KXMLBGAME-26SEP131335KCBOS-BOS -> KXMLBGAME-26SEP131335KCBOS."""
    return market_ticker.rsplit("-", 1)[0]


def _select_combo_legs(real_trading_leagues):
    """
    Picks the best available legs for a real combo: pending strong-tier
    moneyline picks in a league that's CURRENTLY turned on for real
    trading (never combos a paper-only league's picks with real money),
    ranked by probability (highest first -- same reasoning as the paper
    combo builder: this maximizes combined probability for a given leg
    count). Returns up to COMBO_MAX_LEGS legs, or [] if fewer than
    COMBO_MIN_LEGS qualify.
    """
    data = pt.load_paper_trades()
    leagues = {lg.lower() for lg in real_trading_leagues}
    candidates = [
        p for p in data.get("moneyline", [])
        if p.get("status") == "pending"
        and p.get("bet_tier") == "strong"
        and p.get("kalshi_ticker")
        and (p.get("league") or "").lower() in leagues
    ]
    candidates.sort(key=lambda p: p.get("market_probability") or 0, reverse=True)
    legs = candidates[:COMBO_MAX_LEGS]
    if len(legs) < COMBO_MIN_LEGS:
        return []
    return legs


def try_execute_real_combo(client, real_trading_leagues, send_discord_fn, webhook):
    """
    Runs once per cycle when enabled (call from worker.py's real-trading
    path). Picks the best qualifying strong-tier legs, gets or creates a
    real Kalshi combo market for them, submits an RFQ, watches briefly
    for a live quote that clears COMBO_MIN_EDGE, and if one appears,
    places a real IOC buy order to capture it before it disappears.

    Never raises. Returns the filled Order, or None if disabled, too few
    qualifying legs, insufficient budget, or no good quote showed up in
    the watch window.
    """
    if not COMBO_REAL_TRADING_ENABLED:
        return None

    import worker  # deferred -- worker.py imports this module, avoids a circular import

    try:
        legs = _select_combo_legs(real_trading_leagues)
        if not legs:
            return None

        combined_prob = 1.0
        for leg in legs:
            combined_prob *= leg["market_probability"]

        available = ledger.get_available_budget(client, worker.load_open_positions())
        if COMBO_STAKE_DOLLARS > available:
            print(f"[combo] available budget (${available:.2f}) below combo stake (${COMBO_STAKE_DOLLARS:.2f}) -- skipping")
            return None

        selected_markets = [
            {
                "market_ticker": leg["kalshi_ticker"],
                "event_ticker": _event_ticker_from_market_ticker(leg["kalshi_ticker"]),
                "side": "yes",
            }
            for leg in legs
        ]

        collection = client.get_mve_collection(COMBO_COLLECTION_TICKER)
        combo_ticker = None
        try:
            found = collection.lookup_ticker(selected_markets)
            combo_ticker = found.get("market_ticker") or found.get("ticker")
        except Exception:
            combo_ticker = None

        if not combo_ticker:
            market = collection.create_market(selected_markets)
            combo_ticker = market.ticker

        client.communications.create_rfq(
            market_ticker=combo_ticker,
            target_cost_dollars=f"{COMBO_STAKE_DOLLARS:.2f}",
        )

        max_price = max(0.01, combined_prob - COMBO_MIN_EDGE)
        deadline = time.time() + COMBO_RFQ_WATCH_SECONDS
        filled_order = None
        fill_price = None

        while time.time() < deadline:
            time.sleep(COMBO_RFQ_POLL_INTERVAL_SECONDS)
            try:
                market = client.get_market(combo_ticker)
            except Exception:
                continue
            ask = getattr(market, "yes_ask_dollars", None)
            if ask is None:
                continue
            ask = float(ask)
            if not (0 < ask <= max_price):
                continue

            count_fp = max(1.0, COMBO_STAKE_DOLLARS / ask)
            try:
                order = client.portfolio.place_order(
                    combo_ticker, Action.BUY, Side.YES,
                    count_fp=str(round(count_fp, 2)),
                    yes_price_dollars=f"{ask:.4f}",
                    time_in_force=TimeInForce.IOC,
                    buy_max_cost_dollars=f"{COMBO_STAKE_DOLLARS:.2f}",
                )
            except Exception as e:
                print(f"[combo] order placement failed at ${ask:.4f}: {e}")
                continue

            filled_fp = float(getattr(order, "fill_count_fp", None) or 0)
            if filled_fp > 0:
                filled_order = order
                fill_price = ask
                break

        if not filled_order:
            print(f"[combo] no fillable quote (needed <= ${max_price:.4f}, true combined prob {combined_prob*100:.1f}%) "
                  f"within {COMBO_RFQ_WATCH_SECONDS:.0f}s watch window -- giving up this cycle")
            return None

        filled_fp = float(getattr(filled_order, "fill_count_fp", None) or 0)
        positions = worker.load_open_positions()
        positions[combo_ticker] = {
            "entry_price": fill_price, "count_fp": filled_fp,
            "side": "YES", "opened_at": datetime.now().isoformat(),
            "is_combo": True, "combo_legs": [l["picked_team"] for l in legs],
        }
        worker.save_open_positions(positions)

        msg = (
            f"[REAL COMBO FILLED] {len(legs)}-leg combo @ ${fill_price:.4f} "
            f"({', '.join(l['picked_team'] for l in legs)}) -- "
            f"staked ${fill_price*filled_fp:.2f}, pays +${(1.0-fill_price)*filled_fp:.2f} if all legs hit"
        )
        send_discord_fn(webhook, msg)
        return filled_order
    except Exception as e:
        print(f"[combo] try_execute_real_combo error: {e}")
        return None
