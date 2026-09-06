import os
import time
import json
from datetime import datetime, date
from collections import defaultdict

import requests
from pykalshi import KalshiClient, Action, Side, MarketStatus

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])

SHARPAPI_KEY = os.environ["SHARPAPI_KEY"]
DISCORD_WEBHOOK_BETS = os.environ["DISCORD_WEBHOOK_BETS"]
DISCORD_WEBHOOK_UPDATES = os.environ["DISCORD_WEBHOOK_UPDATES"]

MAX_STAKE_PER_TRADE = float(os.getenv("MAX_STAKE_PER_TRADE", "2.00"))
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP", "5.00"))
MIN_EDGE_PCT = 2.0
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1/odds"
DAILY_STATE_FILE = "daily_trading_state.json"
SEEN_TRADES_FILE = "seen_trades.json"


def send_discord(webhook_url, message):
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=5)
        if resp.status_code != 204:
            print(f"[discord] non-204: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[discord] send failed: {e}")


def load_daily_state():
    today = date.today().isoformat()
    if os.path.exists(DAILY_STATE_FILE):
        with open(DAILY_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    return {"date": today, "realized_loss": 0.0, "trades_executed": 0, "halted": False}


def save_daily_state(state):
    with open(DAILY_STATE_FILE, "w") as f:
        json.dump(state, f)


def daily_cap_exceeded(state):
    return state["realized_loss"] >= DAILY_LOSS_CAP or state.get("halted", False)


def load_seen_trades():
    if os.path.exists(SEEN_TRADES_FILE):
        with open(SEEN_TRADES_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_trades(seen):
    with open(SEEN_TRADES_FILE, "w") as f:
        json.dump(list(seen), f)


def remove_vig_two_way(prob_a, prob_b):
    total = prob_a + prob_b
    if total == 0:
        return None, None
    return prob_a / total, prob_b / total


def fetch_sharpapi_odds(league):
    resp = requests.get(
        SHARPAPI_BASE,
        params={"league": league, "market": "main", "limit": 200},
        headers={"X-API-Key": SHARPAPI_KEY},
    )
    if resp.status_code != 200:
        print(f"[sharpapi] {league} failed: {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json().get("data", [])


def find_moneyline_edges(league):
    rows = fetch_sharpapi_odds(league)
    grouped = defaultdict(dict)
    for row in rows:
        if row.get("is_main_line") is not True:
            continue
        if row.get("market_type") != "MONEYLINE":
            continue
        key = (row.get("event_id"), row.get("selection"))
        grouped[key][row.get("sportsbook")] = row

    by_event = defaultdict(set)
    for (event_id, selection) in grouped.keys():
        by_event[event_id].add(selection)

    edges = []
    for event_id, selections in by_event.items():
        selections = list(selections)
        if len(selections) != 2:
            continue
        sel_a, sel_b = selections
        rows_a = grouped[(event_id, sel_a)]
        rows_b = grouped[(event_id, sel_b)]
        common_books = set(rows_a.keys()) & set(rows_b.keys())
        if len(common_books) < 2:
            continue

        novig_a, novig_b = {}, {}
        for book in common_books:
            pa = rows_a[book].get("odds_probability")
            pb = rows_b[book].get("odds_probability")
            if pa is None or pb is None:
                continue
            na, nb = remove_vig_two_way(pa, pb)
            if na is None:
                continue
            novig_a[book] = na
            novig_b[book] = nb

        if not novig_a:
            continue

        fair_a = sum(novig_a.values()) / len(novig_a)
        fair_b = sum(novig_b.values()) / len(novig_b)

        for selection, fair_prob, rows_dict in [(sel_a, fair_a, rows_a), (sel_b, fair_b, rows_b)]:
            best_row = list(rows_dict.values())[0]
            edges.append({
                "league": league.upper(),
                "event_id": event_id,
                "away_team": best_row.get("away_team"),
                "home_team": best_row.get("home_team"),
                "event_start_time": best_row.get("event_start_time"),
                "selection": selection,
                "fair_prob": fair_prob,
            })

    return edges


def get_open_nfl_moneyline_markets(client):
    return client.get_markets(series_ticker="KXNFLGAME", status=MarketStatus.OPEN, limit=1000)


def find_kalshi_match(markets, team_full_name):
    for m in markets:
        title = (m.title or "").replace(" wins", "").strip()
        if title and title.upper() in team_full_name.upper():
            return m
    return None


def execute_kalshi_trade(client, ticker, price_dollars, count_fp, discord_msg):
    state = load_daily_state()
    if daily_cap_exceeded(state):
        print(f"Daily loss cap reached — skipping {ticker}")
        return False

    stake = price_dollars * count_fp
    if stake > MAX_STAKE_PER_TRADE:
        count_fp = max(1, MAX_STAKE_PER_TRADE / price_dollars)
        stake = price_dollars * count_fp

    send_discord(DISCORD_WEBHOOK_BETS, discord_msg + f"\nStake: ${stake:.2f}")

    try:
        order = client.portfolio.place_order(
            ticker,
            Action.BUY,
            Side.YES,
            count_fp=str(round(count_fp, 2)),
            yes_price_dollars=f"{price_dollars:.4f}",
        )
        print(f"✅ Trade executed: {ticker} x{count_fp}")
        send_discord(DISCORD_WEBHOOK_BETS, f"✅ Executed: `{ticker}` x{count_fp}")
        state["trades_executed"] += 1
        save_daily_state(state)
        return True
    except Exception as e:
        err = f"❌ Trade failed for {ticker}: {e}"
        print(err)
        send_discord(DISCORD_WEBHOOK_UPDATES, err)
        return False


def run_once(client, seen_trades):
    send_discord(DISCORD_WEBHOOK_UPDATES, f"Scan starting at {datetime.now().isoformat()}")

    kalshi_markets = get_open_nfl_moneyline_markets(client)
    print(f"Kalshi open NFL moneyline markets: {len(kalshi_markets)}")

    all_edges = find_moneyline_edges("nfl")
    print(f"Moneyline edges found: {len(all_edges)}")

    for edge in all_edges:
        trade_key = f"{edge['event_id']}:{edge['selection']}"
        if trade_key in seen_trades:
            continue

        match = find_kalshi_match(kalshi_markets, edge["selection"])
        if not match:
            continue

        yes_ask = getattr(match, "yes_ask_dollars", None)
        if not yes_ask:
            print(f"No live Kalshi price yet for {match.ticker} — skipping")
            continue

        kalshi_price = float(yes_ask)
        edge_pct = (edge["fair_prob"] - kalshi_price) * 100
        if edge_pct < MIN_EDGE_PCT:
            continue

        msg = (
            f"Matched Edge: {edge['selection']}\n"
            f"Matchup: {edge['away_team']} @ {edge['home_team']}\n"
            f"Kalshi ticker: {match.ticker}\n"
            f"Kalshi price: ${kalshi_price:.2f}\n"
            f"Sharp fair probability: {edge['fair_prob']*100:.1f}%\n"
            f"Edge: +{edge_pct:.2f}%"
        )

        count_fp = max(1.0, MAX_STAKE_PER_TRADE / kalshi_price)
        executed = execute_kalshi_trade(client, match.ticker, kalshi_price, count_fp, msg)
        if executed:
            seen_trades.add(trade_key)
            save_seen_trades(seen_trades)


def main():
    print("--- AR894 Autonomous Worker (NFL Moneyline, pykalshi) ---")
    print(f"MAX_STAKE_PER_TRADE=${MAX_STAKE_PER_TRADE}  DAILY_LOSS_CAP=${DAILY_LOSS_CAP}  interval={SCAN_INTERVAL_SECONDS}s")

    seen_trades = load_seen_trades()
    client = KalshiClient()

    send_discord(DISCORD_WEBHOOK_UPDATES, "Worker started.")
    while True:
        try:
            run_once(client, seen_trades)
        except Exception as e:
            print(f"[loop] error: {e}")
            send_discord(DISCORD_WEBHOOK_UPDATES, f"Worker error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
