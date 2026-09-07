import os
import json
import requests
from datetime import datetime

import os as _os
DATA_DIR = _os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
PAPER_TRADES_FILE = _os.path.join(DATA_DIR, "paper_trades.json")
BTC_PRICE_HISTORY_FILE = _os.path.join(DATA_DIR, "btc_price_history.json")


def load_paper_trades():
    if os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE) as f:
            return json.load(f)
    return {"moneyline": [], "btc": []}


def save_paper_trades(data):
    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Moneyline paper trading
# ---------------------------------------------------------------------------
def make_moneyline_paper_picks(league, sharpapi_rows, kalshi_events, safe_match_fn, send_discord_fn, webhook):
    from collections import defaultdict as dd

    grouped = dd(dict)
    for row in sharpapi_rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        key = (row.get("event_id"), row.get("selection"))
        grouped[key][row.get("sportsbook")] = row

    by_event = dd(set)
    for (event_id, selection) in grouped.keys():
        by_event[event_id].add(selection)

    paper_data = load_paper_trades()
    already_picked = {p["event_id"] for p in paper_data["moneyline"]}
    new_picks = []

    for event_id, selections in by_event.items():
        if event_id in already_picked or len(selections) != 2:
            continue
        sel_a, sel_b = list(selections)
        rows_a, rows_b = grouped[(event_id, sel_a)], grouped[(event_id, sel_b)]
        common_books = set(rows_a.keys()) & set(rows_b.keys())
        if not common_books:
            continue

        probs_a, probs_b = [], []
        for book in common_books:
            pa, pb = rows_a[book].get("odds_probability"), rows_b[book].get("odds_probability")
            if pa is None or pb is None:
                continue
            total = pa + pb
            probs_a.append(pa / total)
            probs_b.append(pb / total)

        if not probs_a:
            continue

        fair_a, fair_b = sum(probs_a) / len(probs_a), sum(probs_b) / len(probs_b)
        picked_selection, picked_prob = (sel_a, fair_a) if fair_a > fair_b else (sel_b, fair_b)
        best_row = list((rows_a if picked_selection == sel_a else rows_b).values())[0]
        away_team, home_team = best_row.get("away_team"), best_row.get("home_team")

        # Find the matching Kalshi ticker and its real current price -- this is
        # what makes P&L tracking possible, not just win/loss.
        match_map = safe_match_fn(kalshi_events, away_team, home_team)
        kalshi_ticker, entry_price = None, None
        if match_map:
            match = match_map.get(picked_selection)
            if match:
                kalshi_ticker = match.ticker
                yes_ask = getattr(match, "yes_ask_dollars", None)
                entry_price = float(yes_ask) if yes_ask else None

        pick = {
            "league": league.upper(),
            "event_id": event_id,
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": picked_selection,
            "market_probability": picked_prob,
            "kalshi_ticker": kalshi_ticker,
            "entry_price": entry_price,
            "picked_at": datetime.now().isoformat(),
            "status": "pending",
        }
        paper_data["moneyline"].append(pick)
        new_picks.append(pick)

    if new_picks:
        save_paper_trades(paper_data)
        for p in new_picks:
            price_note = f"${p['entry_price']:.2f}" if p["entry_price"] else "price unavailable yet"
            msg = (
                f"[PAPER TRADE - {p['league']}] Picked {p['picked_team']}\n"
                f"Matchup: {p['away_team']} @ {p['home_team']}\n"
                f"Market consensus probability: {p['market_probability']*100:.1f}%\n"
                f"Kalshi entry price: {price_note}\n"
                f"(No real money -- tracking for accuracy and hypothetical P&L)"
            )
            send_discord_fn(webhook, msg)

    return new_picks


def resolve_moneyline_paper_trades(client, send_discord_fn=None, webhook=None):
    """
    Checks each pending moneyline pick's Kalshi ticker for a settlement
    result, marks it won/lost, and computes what the actual hypothetical
    P&L would have been using the real entry price captured at pick time.
    """
    paper_data = load_paper_trades()
    changed = False

    for pick in paper_data["moneyline"]:
        if pick["status"] != "pending" or not pick.get("kalshi_ticker"):
            continue

        try:
            market = client.get_market(pick["kalshi_ticker"])
        except Exception:
            continue

        result = getattr(market, "result", None)
        if result not in ("yes", "no"):
            continue  # not settled yet

        won = (result == "yes")
        pick["status"] = "won" if won else "lost"
        pick["resolved_at"] = datetime.now().isoformat()

        if pick.get("entry_price"):
            # Hypothetical: 1 contract at entry_price. Win = payout $1, Loss = lose the stake.
            pick["hypothetical_pnl"] = (1.0 - pick["entry_price"]) if won else -pick["entry_price"]
        else:
            pick["hypothetical_pnl"] = None

        changed = True
        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}" if pick["hypothetical_pnl"] is not None else "N/A (no entry price captured)"
            send_discord_fn(
                webhook,
                f"[RESOLVED - {pick['league']}] {pick['picked_team']}: {pick['status'].upper()} "
                f"(hypothetical P&L: {pnl_str})",
            )

    if changed:
        save_paper_trades(paper_data)
    return changed


# ---------------------------------------------------------------------------
# BTC 15-min paper trading
# ---------------------------------------------------------------------------
def get_btc_spot_price():
    try:
        resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        print(f"[btc] price fetch failed: {e}")
        return None


def load_btc_price_history():
    if os.path.exists(BTC_PRICE_HISTORY_FILE):
        with open(BTC_PRICE_HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_btc_price_history(history):
    history = history[-20:]
    with open(BTC_PRICE_HISTORY_FILE, "w") as f:
        json.dump(history, f)


def make_btc_paper_pick(client, MarketStatus, send_discord_fn, webhook):
    price = get_btc_spot_price()
    if price is None:
        return None

    history = load_btc_price_history()
    history.append({"price": price, "at": datetime.now().isoformat()})
    save_btc_price_history(history)

    if len(history) < 3:
        return None

    momentum = history[-1]["price"] - history[-3]["price"]
    direction = "up" if momentum > 0 else "down"

    try:
        markets = client.get_markets(series_ticker="KXBTC15M", status=MarketStatus.OPEN, limit=5)
    except Exception as e:
        print(f"[btc] market fetch failed: {e}")
        return None

    if not markets:
        return None

    market = sorted(markets, key=lambda m: getattr(m, "close_time", None) or "9999")[0]

    paper_data = load_paper_trades()
    already_picked = {p["ticker"] for p in paper_data["btc"]}
    if market.ticker in already_picked:
        return None

    entry_price = None
    yes_ask = getattr(market, "yes_ask_dollars", None)
    no_ask = getattr(market, "no_ask_dollars", None)
    if direction == "up" and yes_ask:
        entry_price = float(yes_ask)
    elif direction == "down" and no_ask:
        entry_price = float(no_ask)

    pick = {
        "ticker": market.ticker,
        "title": market.title,
        "predicted_direction": direction,
        "btc_price_at_pick": price,
        "momentum_signal": momentum,
        "entry_price": entry_price,
        "picked_at": datetime.now().isoformat(),
        "status": "pending",
    }
    paper_data["btc"].append(pick)
    save_paper_trades(paper_data)

    price_note = f"${entry_price:.2f}" if entry_price else "price unavailable"
    msg = (
        f"[PAPER TRADE - BTC 15min] Predicting: {direction.upper()}\n"
        f"Market: {market.title}\n"
        f"BTC price now: ${price:,.2f} (momentum: {momentum:+.2f})\n"
        f"Entry price: {price_note}\n"
        f"(No real money -- experimental signal, tracking for accuracy and P&L)"
    )
    send_discord_fn(webhook, msg)
    return pick


def resolve_btc_paper_trades(client, send_discord_fn=None, webhook=None):
    paper_data = load_paper_trades()
    changed = False

    for pick in paper_data["btc"]:
        if pick["status"] != "pending":
            continue
        try:
            market = client.get_market(pick["ticker"])
        except Exception:
            continue

        result = getattr(market, "result", None)
        if result not in ("yes", "no"):
            continue

        actual_direction = "up" if result == "yes" else "down"
        won = (actual_direction == pick["predicted_direction"])
        pick["status"] = "won" if won else "lost"
        pick["resolved_at"] = datetime.now().isoformat()

        if pick.get("entry_price"):
            pick["hypothetical_pnl"] = (1.0 - pick["entry_price"]) if won else -pick["entry_price"]
        else:
            pick["hypothetical_pnl"] = None

        changed = True
        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}" if pick["hypothetical_pnl"] is not None else "N/A"
            send_discord_fn(webhook, f"[RESOLVED - BTC] {pick['title']}: {pick['status'].upper()} (hypothetical P&L: {pnl_str})")

    if changed:
        save_paper_trades(paper_data)
    return changed


def get_paper_trade_summary():
    paper_data = load_paper_trades()
    summary = {}
    for category in ["moneyline", "btc"]:
        trades = paper_data[category]
        resolved = [t for t in trades if t["status"] in ("won", "lost")]
        wins = [t for t in resolved if t["status"] == "won"]
        pnls = [t["hypothetical_pnl"] for t in resolved if t.get("hypothetical_pnl") is not None]

        summary[category] = {
            "total_picks": len(trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": (len(wins) / len(resolved) * 100) if resolved else None,
            "total_hypothetical_pnl": sum(pnls) if pnls else None,
            "pnl_sample_size": len(pnls),
        }
    return summary
