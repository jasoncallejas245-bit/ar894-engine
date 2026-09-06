import os
import json
import requests
from datetime import datetime, date
from collections import defaultdict

PAPER_TRADES_FILE = "paper_trades.json"
BTC_PRICE_HISTORY_FILE = "btc_price_history.json"


def load_paper_trades():
    if os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE) as f:
            return json.load(f)
    return {"moneyline": [], "btc": []}


def save_paper_trades(data):
    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Moneyline paper trading: pick the market's own de-vigged consensus favorite
# ---------------------------------------------------------------------------
def make_moneyline_paper_picks(league, sharpapi_rows, kalshi_events, send_discord_fn, webhook):
    """
    For each game, compute the de-vigged consensus probability (averaged
    across whatever books we have) and 'pick' whichever side is favored.
    This is NOT a proprietary edge -- it is the market's own consensus,
    logged honestly as a baseline experiment.
    """
    from collections import defaultdict as dd

    grouped = dd(dict)
    print(f"[paper-ml] {league} got {len(sharpapi_rows)} raw rows")
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
    print(f"[paper-ml] {league} grouped into {len(by_event)} candidate events")

    for event_id, selections in by_event.items():
        if event_id in already_picked or len(selections) != 2:
            continue
        print(f"[paper-ml] event {event_id}: {len(selections)} selections, common_books check next")
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

        pick = {
            "league": league.upper(),
            "event_id": event_id,
            "away_team": best_row.get("away_team"),
            "home_team": best_row.get("home_team"),
            "picked_team": picked_selection,
            "market_probability": picked_prob,
            "picked_at": datetime.now().isoformat(),
            "status": "pending",
        }
        paper_data["moneyline"].append(pick)
        new_picks.append(pick)

    if new_picks:
        save_paper_trades(paper_data)
        for p in new_picks:
            msg = (
                f"[PAPER TRADE - {p['league']}] Picked {p['picked_team']}\n"
                f"Matchup: {p['away_team']} @ {p['home_team']}\n"
                f"Market consensus probability: {p['market_probability']*100:.1f}%\n"
                f"(No real money -- tracking for accuracy)"
            )
            send_discord_fn(webhook, msg)

    return new_picks


def resolve_moneyline_paper_trades(client, get_market_fn):
    """
    Checks pending paper picks against Kalshi's settled market results
    (Kalshi settles the corresponding KXNFLGAME/KXNCAAFGAME market once
    the real game ends) and marks them won/lost.
    """
    paper_data = load_paper_trades()
    resolved_any = False

    for pick in paper_data["moneyline"]:
        if pick["status"] != "pending":
            continue
        # We don't store the Kalshi ticker at pick time in this simple version,
        # so resolution happens via a separate matching pass in the worker.
        pass

    return resolved_any


# ---------------------------------------------------------------------------
# BTC 15-min paper trading: simple momentum signal
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
    # Keep only the last 20 readings -- enough for a short momentum window
    history = history[-20:]
    with open(BTC_PRICE_HISTORY_FILE, "w") as f:
        json.dump(history, f)


def make_btc_paper_pick(client, MarketStatus, send_discord_fn, webhook):
    """
    Simple momentum experiment: if BTC has moved up over the last few
    readings, 'predict' up; if down, predict down. This is a naive
    baseline, explicitly not a validated strategy -- the point of paper
    trading it is to find out honestly whether it does anything at all.
    """
    price = get_btc_spot_price()
    if price is None:
        return None

    history = load_btc_price_history()
    history.append({"price": price, "at": datetime.now().isoformat()})
    save_btc_price_history(history)

    if len(history) < 3:
        return None  # not enough data yet to compute momentum

    momentum = history[-1]["price"] - history[-3]["price"]
    direction = "up" if momentum > 0 else "down"

    try:
        markets = client.get_markets(series_ticker="KXBTC15M", status=MarketStatus.OPEN, limit=5)
    except Exception as e:
        print(f"[btc] market fetch failed: {e}")
        return None

    if not markets:
        return None

    # Nearest-expiry open market
    market = sorted(markets, key=lambda m: getattr(m, "close_time", None) or "9999")[0]

    paper_data = load_paper_trades()
    already_picked = {p["ticker"] for p in paper_data["btc"]}
    if market.ticker in already_picked:
        return None

    pick = {
        "ticker": market.ticker,
        "title": market.title,
        "predicted_direction": direction,
        "btc_price_at_pick": price,
        "momentum_signal": momentum,
        "picked_at": datetime.now().isoformat(),
        "status": "pending",
    }
    paper_data["btc"].append(pick)
    save_paper_trades(paper_data)

    msg = (
        f"[PAPER TRADE - BTC 15min] Predicting: {direction.upper()}\n"
        f"Market: {market.title}\n"
        f"BTC price now: ${price:,.2f} (momentum: {momentum:+.2f} over last readings)\n"
        f"(No real money -- experimental signal, tracking for accuracy)"
    )
    send_discord_fn(webhook, msg)
    return pick


def resolve_btc_paper_trades(client):
    """Checks pending BTC picks against settled market results."""
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
        if result in ("yes", "no"):
            actual_direction = "up" if result == "yes" else "down"
            pick["status"] = "won" if actual_direction == pick["predicted_direction"] else "lost"
            pick["resolved_at"] = datetime.now().isoformat()
            changed = True

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
        summary[category] = {
            "total_picks": len(trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": (len(wins) / len(resolved) * 100) if resolved else None,
        }
    return summary
