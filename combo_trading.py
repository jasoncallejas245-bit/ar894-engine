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
#   - IMPORTANT TRADEOFF (confirmed with the user 2026-09-13): an RFQ
#     combo has a real counterparty (whichever market maker quoted it),
#     unlike a normal Kalshi market's anonymous shared order book. And
#     there's no mid-game exit -- once a quote is accepted, there's no
#     ongoing liquidity to sell back into, so the position is genuinely
#     locked in until the true final outcome. Regular solo moneyline
#     positions don't have this problem (see worker.py's
#     check_and_close_profitable_positions, which already does live
#     price-based early exits on those) -- this tradeoff is specific to
#     combos, accepted deliberately in exchange for the bigger payout.
#     check_and_close_profitable_positions explicitly skips any position
#     tagged is_combo=True for exactly this reason -- never attempt an
#     early exit on a combo, it doesn't make sense structurally.
#
# OFF by default (COMBO_REAL_TRADING_ENABLED=false). Turning this on lets
# the bot spend real money placing real IOC orders against live RFQ
# quotes. Read every comment in this file before enabling in production.
# ---------------------------------------------------------------------------

COMBO_REAL_TRADING_ENABLED = os.getenv("COMBO_REAL_TRADING_ENABLED", "false").lower() == "true"

COMBO_COLLECTION_TICKER = os.getenv("COMBO_COLLECTION_TICKER", "KXMVECROSSCATEGORY-R")

# Fixed at exactly 2 legs, at the user's explicit request (2026-09-13) --
# combo bets should only ever be 2 moneylines, nothing more.
COMBO_MIN_LEGS = int(os.getenv("COMBO_MIN_LEGS", "2"))
COMBO_MAX_LEGS = int(os.getenv("COMBO_MAX_LEGS", "2"))
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

# In-memory de-dupe so we don't hammer create_rfq for the same combo market
# faster than Kalshi allows. The same top-ranked strong-tier legs get
# re-selected every fast cycle (the pending-picks list barely changes
# minute to minute), so without this we'd resubmit an RFQ for the exact
# same combo_ticker on every cycle -- and Kalshi rejects a second RFQ on a
# market that still has one open with a 409 "already exists". Not
# persisted across restarts; worst case after a restart is one harmless
# duplicate-RFQ attempt, caught below anyway.
_last_rfq_at = {}
COMBO_RFQ_COOLDOWN_SECONDS = float(os.getenv("COMBO_RFQ_COOLDOWN_SECONDS", "120"))


def _create_rfq_deduped(client, combo_ticker, target_cost_dollars):
    """
    Submits an RFQ for combo_ticker, but skips submitting a new one if we
    already did for this exact combo within COMBO_RFQ_COOLDOWN_SECONDS --
    there's still one live, nothing to do. If Kalshi rejects it anyway
    with "already exists" (e.g. right after a process restart, before our
    in-memory cooldown knows about it), that's not a real failure either --
    it just confirms one is already live -- so it's treated the same way,
    not re-raised.
    """
    now = time.time()
    if now - _last_rfq_at.get(combo_ticker, 0) < COMBO_RFQ_COOLDOWN_SECONDS:
        return
    try:
        client.communications.create_rfq(
            market_ticker=combo_ticker,
            target_cost_dollars=target_cost_dollars,
        )
    except Exception as e:
        if "already_exists" not in str(e) and "already exists" not in str(e):
            raise
    _last_rfq_at[combo_ticker] = now


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

        _create_rfq_deduped(client, combo_ticker, f"{COMBO_STAKE_DOLLARS:.2f}")

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


# ---------------------------------------------------------------------------
# Paper/dry-run combo trading (NEW 2026-09-13, at the user's request: every
# other strategy in this bot gets paper-tested before real money is ever
# risked on it -- the real combo trading above didn't, which was a real gap
# against this project's own pattern. Closed here.
#
# This uses the REAL Kalshi combo market + RFQ system to get a genuine live
# quote -- create_market/create_rfq never move money or place an order, so
# this is exactly as safe as reading a price. It just stops short of the
# final place_order step and records the result as a paper position
# instead. Runs regardless of COMBO_REAL_TRADING_ENABLED or
# REAL_TRADING_LEAGUES -- paper trading exists specifically to validate a
# strategy BEFORE real trading starts, so it shouldn't be gated behind the
# same switches that gate real money.
#
# Side benefit: every dry-run cycle also passively gathers evidence on the
# open "can a combo be exited early via a sell-side RFQ" question, by
# recording whenever yes_bid_dollars is ever seen above 0 during the watch
# window (a live buy-back quote, not just a sell-to-you ask). No separate
# test setup needed for that -- it accumulates on its own as this runs.
# ---------------------------------------------------------------------------

PAPER_COMBO_ENABLED = os.getenv("COMBO_PAPER_ENABLED", "true").lower() == "true"


def _select_combo_legs_paper():
    """
    Same idea as _select_combo_legs, but NOT gated on REAL_TRADING_LEAGUES
    -- paper testing should work for any strong-tier pick regardless of
    whether real trading is on for that league yet, since the whole point
    is validating the strategy before real money is involved.
    """
    data = pt.load_paper_trades()
    candidates = [
        p for p in data.get("moneyline", [])
        if p.get("status") == "pending" and p.get("bet_tier") == "strong" and p.get("kalshi_ticker")
    ]
    candidates.sort(key=lambda p: p.get("market_probability") or 0, reverse=True)
    legs = candidates[:COMBO_MAX_LEGS]
    if len(legs) < COMBO_MIN_LEGS:
        return []
    return legs


def try_paper_combo_dry_run(client, send_discord_fn=None, webhook=None):
    """
    Runs once per cycle when enabled. Picks the best qualifying strong-tier
    legs (same selection as the real version), gets/creates the real combo
    market, submits a real RFQ, and watches briefly for a live quote --
    exactly like try_execute_real_combo, but records whatever quote (or
    lack of one) it sees as a paper ticket instead of buying anything.
    Never raises. Returns the ticket dict, or None if disabled, too few
    legs, or the RFQ/market calls themselves fail.
    """
    if not PAPER_COMBO_ENABLED:
        return None
    try:
        legs = _select_combo_legs_paper()
        if not legs:
            return None

        combined_prob = 1.0
        for leg in legs:
            combined_prob *= leg["market_probability"]

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

        _create_rfq_deduped(client, combo_ticker, f"{COMBO_STAKE_DOLLARS:.2f}")

        deadline = time.time() + COMBO_RFQ_WATCH_SECONDS
        quote_seen = None
        bid_ever_seen = False
        best_bid_seen = 0.0
        while time.time() < deadline:
            time.sleep(COMBO_RFQ_POLL_INTERVAL_SECONDS)
            try:
                market = client.get_market(combo_ticker)
            except Exception:
                continue
            ask = getattr(market, "yes_ask_dollars", None)
            bid = getattr(market, "yes_bid_dollars", None)
            if bid is not None and float(bid) > 0:
                bid_ever_seen = True
                best_bid_seen = max(best_bid_seen, float(bid))
            if ask is not None and 0 < float(ask) < 1.0 and quote_seen is None:
                quote_seen = float(ask)

        data = pt.load_paper_trades()
        data.setdefault("combo_dryrun", [])
        ticket = {
            "ticket_id": f"combo-dryrun-{datetime.now().isoformat()}",
            "combo_ticker": combo_ticker,
            "legs": [l["picked_team"] for l in legs],
            "leg_tickers": [l["kalshi_ticker"] for l in legs],
            "true_combined_prob": round(combined_prob, 4),
            "quote_seen": quote_seen,
            "bid_ever_seen": bid_ever_seen,  # evidence toward the early-exit question
            "best_bid_seen": round(best_bid_seen, 4) if bid_ever_seen else None,
            "stake_dollars": COMBO_STAKE_DOLLARS,
            "picked_at": datetime.now().isoformat(),
            "status": "pending" if quote_seen else "no_quote",
        }
        data["combo_dryrun"].append(ticket)
        pt.save_paper_trades(data)

        if send_discord_fn and webhook and quote_seen:
            send_discord_fn(
                webhook,
                f"[PAPER COMBO] {len(legs)}-leg dry-run quote: ${quote_seen:.4f} "
                f"({', '.join(l['picked_team'] for l in legs)}), true combined prob {combined_prob*100:.1f}%"
                + (f" -- also saw a live BID of ${best_bid_seen:.4f} (early-exit evidence!)" if bid_ever_seen else "")
            )
        return ticket
    except Exception as e:
        print(f"[combo] try_paper_combo_dry_run error: {e}")
        return None


def resolve_paper_combo_dryrun():
    """
    Checks pending paper combo dry-run tickets against each leg's own
    already-resolved moneyline status (same all-or-nothing logic the old
    parlay resolver used) -- more reliable than re-querying the combo
    market itself later, since that market's live pricing is ephemeral by
    nature. Never raises.
    """
    try:
        data = pt.load_paper_trades()
        moneyline_by_ticker = {p.get("kalshi_ticker"): p for p in data.get("moneyline", []) if p.get("kalshi_ticker")}
        changed = False
        for t in data.get("combo_dryrun", []):
            if t["status"] != "pending":
                continue
            statuses = [moneyline_by_ticker.get(tk, {}).get("status") for tk in t.get("leg_tickers", [])]
            if any(s is None for s in statuses):
                continue  # a leg's underlying pick vanished -- can't resolve
            if any(s == "pending" for s in statuses):
                continue  # still waiting on at least one leg
            won = all(s == "won" for s in statuses)
            t["status"] = "won" if won else "lost"
            t["resolved_at"] = datetime.now().isoformat()
            if t.get("quote_seen"):
                price = t["quote_seen"]
                contracts = max(1.0, t["stake_dollars"] / price)
                t["hypothetical_pnl"] = round((1.0 - price) * contracts, 2) if won else round(-t["stake_dollars"], 2)
            changed = True
        if changed:
            pt.save_paper_trades(data)
    except Exception as e:
        print(f"[combo] resolve_paper_combo_dryrun error: {e}")
