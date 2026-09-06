import os
import time
import json
import tempfile
from datetime import datetime, date
from collections import defaultdict

import requests
from pykalshi import KalshiClient, Action, Side, MarketStatus

if os.getenv("KALSHI_PRIVATE_KEY_CONTENT"):
    _key_file = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    _key_file.write(os.environ["KALSHI_PRIVATE_KEY_CONTENT"])
    _key_file.close()
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = _key_file.name

os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])

SHARPAPI_KEY = os.environ["SHARPAPI_KEY"]
DISCORD_WEBHOOK_BETS = os.environ["DISCORD_WEBHOOK_BETS"]
DISCORD_WEBHOOK_UPDATES = os.environ["DISCORD_WEBHOOK_UPDATES"]

MAX_STAKE_PER_TRADE = float(os.getenv("MAX_STAKE_PER_TRADE", "5.00"))
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP", "5.00"))
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "20.0"))
MIN_EDGE_PCT = 2.0
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1/odds"
DAILY_STATE_FILE = "daily_trading_state.json"
SEEN_TRADES_FILE = "seen_trades.json"
OPEN_POSITIONS_FILE = "open_positions.json"

# league -> Kalshi series ticker for moneyline (game winner) markets
LEAGUE_SERIES = {
    "nfl": "KXNFLGAME",
    "ncaaf": "KXNCAAFGAME",
}




# --- Jarvis-style voice for Discord messages ---
import random

_SCAN_LINES = [
    "Running the numbers now, sir.",
    "Scanning the boards. One moment.",
    "Let's see what the markets are hiding today.",
    "Sweeping NFL and NCAAF for anything worth your attention.",
]

_NO_EDGE_LINES = [
    "Nothing worth acting on this cycle. The books are behaving themselves.",
    "Quiet out there — no real edges detected.",
    "Markets are efficient at the moment. Standing by.",
]

_EDGE_FOUND_PREFIX = "I've found something, sir. "
_TRADE_EXECUTED_PREFIX = "Done. Order placed: "
_POSITION_CLOSED_PREFIX = "Took the profit while it was there. "
_ERROR_PREFIX = "Small hiccup — nothing to worry about, but you should know: "

def jarvis(message, category="info"):
    if category == "scan_start":
        return f"{random.choice(_SCAN_LINES)}"
    if category == "no_edge":
        return f"{random.choice(_NO_EDGE_LINES)}"
    if category == "edge_found":
        return _EDGE_FOUND_PREFIX + message
    if category == "trade_executed":
        return _TRADE_EXECUTED_PREFIX + message
    if category == "position_closed":
        return _POSITION_CLOSED_PREFIX + message
    if category == "error":
        return _ERROR_PREFIX + message
    return message


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


def load_open_positions():
    if os.path.exists(OPEN_POSITIONS_FILE):
        with open(OPEN_POSITIONS_FILE) as f:
            return json.load(f)
    return {}


def save_open_positions(positions):
    with open(OPEN_POSITIONS_FILE, "w") as f:
        json.dump(positions, f)


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


def get_open_markets(client, series_ticker):
    return client.get_markets(series_ticker=series_ticker, status=MarketStatus.OPEN, limit=1000)


def group_kalshi_markets_by_event(markets):
    """Groups markets by event_ticker, e.g. KXNCAAFGAME-26SEP19PURUCLA -> [market_UCLA, market_PUR]"""
    events = defaultdict(list)
    for m in markets:
        event_ticker = getattr(m, "event_ticker", None)
        if event_ticker:
            events[event_ticker].append(m)
    return events


def short_name(kalshi_title):
    return (kalshi_title or "").replace(" wins", "").strip().upper()


def safe_match_event(kalshi_events, away_team, home_team):
    """
    Finds the Kalshi event whose two team markets correctly and
    UNAMBIGUOUSLY pair up with (away_team, home_team). Returns
    {team_full_name: market} on success, or None if no safe match
    is found (better to skip a trade than risk matching the wrong team,
    e.g. 'Washington' vs 'Washington State').
    """
    away_upper = away_team.upper()
    home_upper = home_team.upper()

    for event_ticker, markets in kalshi_events.items():
        if len(markets) != 2:
            continue

        m1, m2 = markets
        s1, s2 = short_name(m1.title), short_name(m2.title)

        # Try both possible pairings and check for exclusivity
        pairings = [
            {(s1, away_upper), (s2, home_upper)},
            {(s1, home_upper), (s2, away_upper)},
        ]

        def pairing_is_safe(short, full, other_full):
            # short must be found in its assigned full name...
            if short not in full:
                return False
            # ...and NOT be a confusingly-also-valid substring of the other team
            if short in other_full:
                return False
            return True

        # Pairing 1: s1<->away, s2<->home
        if pairing_is_safe(s1, away_upper, home_upper) and pairing_is_safe(s2, home_upper, away_upper):
            return {away_team: m1, home_team: m2}

        # Pairing 2: s1<->home, s2<->away
        if pairing_is_safe(s1, home_upper, away_upper) and pairing_is_safe(s2, away_upper, home_upper):
            return {home_team: m1, away_team: m2}

    return None


def execute_kalshi_buy(client, ticker, price_dollars, count_fp, discord_msg):
    state = load_daily_state()
    if daily_cap_exceeded(state):
        print(f"Daily loss cap reached — skipping {ticker}")
        return False

    stake = price_dollars * count_fp
    if stake > MAX_STAKE_PER_TRADE:
        count_fp = max(1.0, MAX_STAKE_PER_TRADE / price_dollars)
        stake = price_dollars * count_fp

    send_discord(DISCORD_WEBHOOK_BETS, jarvis(discord_msg + f"\nStake: ${stake:.2f}", "edge_found"))

    try:
        client.portfolio.place_order(
            ticker,
            Action.BUY,
            Side.YES,
            count_fp=str(round(count_fp, 2)),
            yes_price_dollars=f"{price_dollars:.4f}",
        )
        print(f"Trade executed: {ticker} x{count_fp}")
        send_discord(DISCORD_WEBHOOK_BETS, jarvis(f"{ticker} x{count_fp:.2f} @ ${price_dollars:.2f}", "trade_executed"))
        state["trades_executed"] += 1
        save_daily_state(state)

        positions = load_open_positions()
        positions[ticker] = {
            "entry_price": price_dollars,
            "count_fp": count_fp,
            "opened_at": datetime.now().isoformat(),
        }
        save_open_positions(positions)
        return True
    except Exception as e:
        err = f"Trade failed for {ticker}: {e}"
        print(err)
        send_discord(DISCORD_WEBHOOK_UPDATES, err)
        return False


def check_and_close_profitable_positions(client):
    positions = load_open_positions()
    if not positions:
        return

    for ticker, pos in list(positions.items()):
        try:
            market = client.get_market(ticker)
        except Exception as e:
            print(f"Could not check position {ticker}: {e}")
            continue

        current_bid = getattr(market, "yes_bid_dollars", None)
        if not current_bid:
            continue

        current_bid = float(current_bid)
        entry_price = pos["entry_price"]
        gain_pct = ((current_bid - entry_price) / entry_price) * 100

        if gain_pct >= PROFIT_TARGET_PCT:
            try:
                client.portfolio.place_order(
                    ticker,
                    Action.SELL,
                    Side.YES,
                    count_fp=str(pos["count_fp"]),
                    yes_price_dollars=f"{current_bid:.4f}",
                )
                profit = (current_bid - entry_price) * pos["count_fp"]
                msg = (
                    f"Closed position: {ticker}\n"
                    f"Entry: ${entry_price:.2f} -> Exit: ${current_bid:.2f}\n"
                    f"Gain: +{gain_pct:.1f}% (${profit:.2f})"
                )
                print(msg)
                send_discord(DISCORD_WEBHOOK_BETS, jarvis(msg, "position_closed"))
                del positions[ticker]
                save_open_positions(positions)
            except Exception as e:
                err = f"Failed to close profitable position {ticker}: {e}"
                print(err)
                send_discord(DISCORD_WEBHOOK_UPDATES, err)


def process_league(client, league, seen_trades):
    series_ticker = LEAGUE_SERIES[league]
    kalshi_markets = get_open_markets(client, series_ticker)
    kalshi_events = group_kalshi_markets_by_event(kalshi_markets)
    print(f"[{league}] Kalshi open events: {len(kalshi_events)}")

    edges = find_moneyline_edges(league)
    print(f"[{league}] Moneyline edges found: {len(edges)}")

    # Group edges by event_id so we only do the event-matching lookup once per game
    edges_by_event = defaultdict(list)
    for e in edges:
        edges_by_event[e["event_id"]].append(e)

    for event_id, event_edges in edges_by_event.items():
        away_team = event_edges[0]["away_team"]
        home_team = event_edges[0]["home_team"]

        match_map = safe_match_event(kalshi_events, away_team, home_team)
        if not match_map:
            continue  # no safe, unambiguous match found — skip rather than risk a wrong trade

        for edge in event_edges:
            trade_key = f"{edge['event_id']}:{edge['selection']}"
            if trade_key in seen_trades:
                continue

            match = match_map.get(edge["selection"])
            if not match:
                continue

            yes_ask = getattr(match, "yes_ask_dollars", None)
            if not yes_ask:
                continue

            kalshi_price = float(yes_ask)
            edge_pct = (edge["fair_prob"] - kalshi_price) * 100
            if edge_pct < MIN_EDGE_PCT:
                continue

            msg = (
                f"[{league.upper()}] Matched Edge: {edge['selection']}\n"
                f"Matchup: {edge['away_team']} @ {edge['home_team']}\n"
                f"Kalshi ticker: {match.ticker}\n"
                f"Kalshi price: ${kalshi_price:.2f}\n"
                f"Sharp fair probability: {edge['fair_prob']*100:.1f}%\n"
                f"Edge: +{edge_pct:.2f}%"
            )

            count_fp = max(1.0, MAX_STAKE_PER_TRADE / kalshi_price)
            executed = execute_kalshi_buy(client, match.ticker, kalshi_price, count_fp, msg)
            if executed:
                seen_trades.add(trade_key)
                save_seen_trades(seen_trades)


def run_once(client, seen_trades):
    send_discord(DISCORD_WEBHOOK_UPDATES, jarvis("", "scan_start"))

    check_and_close_profitable_positions(client)

    for league in LEAGUE_SERIES.keys():
        try:
            process_league(client, league, seen_trades)
        except Exception as e:
            print(f"[{league}] error: {e}")
            send_discord(DISCORD_WEBHOOK_UPDATES, f"[{league}] scan error: {e}")


def main():
    print("--- AR894 Autonomous Worker (NFL + NCAAF Moneyline, pykalshi) ---")
    print(f"MAX_STAKE_PER_TRADE=${MAX_STAKE_PER_TRADE}  DAILY_LOSS_CAP=${DAILY_LOSS_CAP}  PROFIT_TARGET_PCT={PROFIT_TARGET_PCT}%  interval={SCAN_INTERVAL_SECONDS}s")

    seen_trades = load_seen_trades()
    client = KalshiClient()

    send_discord(DISCORD_WEBHOOK_UPDATES, "Systems online. NFL and NCAAF under watch. I'll let you know the moment something interesting turns up.")
    while True:
        try:
            run_once(client, seen_trades)
        except Exception as e:
            print(f"[loop] error: {e}")
            send_discord(DISCORD_WEBHOOK_UPDATES, jarvis(str(e), "error"))
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
