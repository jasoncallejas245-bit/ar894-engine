import os
import time
import json
import random
import tempfile
from datetime import datetime, date
from collections import defaultdict

import requests
from pykalshi import KalshiClient, Action, Side, MarketStatus

import paper_trading as pt

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
LAST_SUMMARY_FILE = "last_summary.json"

LEAGUE_SERIES = {
    "nfl": "KXNFLGAME",
    "ncaaf": "KXNCAAFGAME",
}

_EDGE_FOUND_PREFIX = "I've found something, sir. "
_TRADE_EXECUTED_PREFIX = "Done. Order placed: "
_POSITION_CLOSED_PREFIX = "Took the profit while it was there. "
_ERROR_PREFIX = "Small hiccup, sir — nothing to worry about, but you should know: "


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


def find_moneyline_edges(rows):
    grouped = defaultdict(dict)
    for row in rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
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
        rows_a, rows_b = grouped[(event_id, sel_a)], grouped[(event_id, sel_b)]
        common_books = set(rows_a.keys()) & set(rows_b.keys())
        if len(common_books) < 2:
            continue

        novig_a, novig_b = {}, {}
        for book in common_books:
            pa, pb = rows_a[book].get("odds_probability"), rows_b[book].get("odds_probability")
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
    events = defaultdict(list)
    for m in markets:
        event_ticker = getattr(m, "event_ticker", None)
        if event_ticker:
            events[event_ticker].append(m)
    return events


def normalize_team_name(name):
    """
    Normalizes team names for EXACT comparison (never substring/containment,
    which incorrectly matches e.g. 'Texas' inside 'Texas State'). Handles the
    'St.' vs 'State' abbreviation difference between Kalshi and SharpAPI.
    """
    n = (name or "").upper().strip()
    n = n.replace(" ST.", " STATE").replace(" ST ", " STATE ")
    if n.endswith(" ST"):
        n = n[:-3] + " STATE"
    n = n.replace(".", "").replace("  ", " ")
    return n.strip()


def short_name(kalshi_title):
    return (kalshi_title or "").replace(" wins", "").strip()


def safe_match_event(kalshi_events, away_team, home_team):
    """
    Matches on EXACT normalized name equality only. If a school name is a
    prefix of another real school's name (Texas / Texas State, Miami / Miami
    OH, etc.), containment matching can silently pair the wrong real-world
    game -- exact match means we simply skip the trade instead of guessing.
    """
    away_norm, home_norm = normalize_team_name(away_team), normalize_team_name(home_team)

    for event_ticker, markets in kalshi_events.items():
        if len(markets) != 2:
            continue
        m1, m2 = markets
        s1_norm, s2_norm = normalize_team_name(short_name(m1.title)), normalize_team_name(short_name(m2.title))

        if s1_norm == away_norm and s2_norm == home_norm:
            return {away_team: m1, home_team: m2}
        if s1_norm == home_norm and s2_norm == away_norm:
            return {home_team: m1, away_team: m2}

    return None


TRADE_AUDIT_LOG = "trade_audit_log.json"

def log_trade_decision(ticker, price_dollars, count_fp, fair_prob, edge_pct, matchup, league):
    entry = {
        "ticker": ticker, "league": league, "matchup": matchup,
        "kalshi_price": price_dollars, "fair_prob": fair_prob, "edge_pct": edge_pct,
        "count_fp": count_fp, "stake": price_dollars * count_fp,
        "decided_at": datetime.now().isoformat(),
    }
    log = []
    if os.path.exists(TRADE_AUDIT_LOG):
        with open(TRADE_AUDIT_LOG) as f:
            log = json.load(f)
    log.append(entry)
    with open(TRADE_AUDIT_LOG, "w") as f:
        json.dump(log, f, indent=2)


def execute_kalshi_buy(client, ticker, price_dollars, count_fp, discord_msg, fair_prob=None, edge_pct=None, matchup=None, league=None):
    state = load_daily_state()
    if daily_cap_exceeded(state):
        print(f"Daily loss cap reached — skipping {ticker}")
        return False

    if fair_prob is not None:
        log_trade_decision(ticker, price_dollars, count_fp, fair_prob, edge_pct, matchup, league)

    stake = price_dollars * count_fp
    if stake > MAX_STAKE_PER_TRADE:
        count_fp = max(1.0, MAX_STAKE_PER_TRADE / price_dollars)
        stake = price_dollars * count_fp

    send_discord(DISCORD_WEBHOOK_BETS, _EDGE_FOUND_PREFIX + discord_msg + f"\nStake: ${stake:.2f}")

    try:
        client.portfolio.place_order(
            ticker, Action.BUY, Side.YES,
            count_fp=str(round(count_fp, 2)),
            yes_price_dollars=f"{price_dollars:.4f}",
        )
        send_discord(DISCORD_WEBHOOK_BETS, _TRADE_EXECUTED_PREFIX + f"{ticker} x{count_fp:.2f} @ ${price_dollars:.2f}")
        state["trades_executed"] += 1
        save_daily_state(state)

        positions = load_open_positions()
        positions[ticker] = {"entry_price": price_dollars, "count_fp": count_fp, "opened_at": datetime.now().isoformat()}
        save_open_positions(positions)
        return True
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"trade failed for {ticker}: {e}")
        return False


def check_and_close_profitable_positions(client):
    positions = load_open_positions()
    if not positions:
        return

    for ticker, pos in list(positions.items()):
        try:
            market = client.get_market(ticker)
        except Exception:
            continue

        current_bid = getattr(market, "yes_bid_dollars", None)
        if not current_bid:
            continue

        current_bid = float(current_bid)
        gain_pct = ((current_bid - pos["entry_price"]) / pos["entry_price"]) * 100

        if gain_pct >= PROFIT_TARGET_PCT:
            try:
                client.portfolio.place_order(
                    ticker, Action.SELL, Side.YES,
                    count_fp=str(pos["count_fp"]),
                    yes_price_dollars=f"{current_bid:.4f}",
                )
                profit = (current_bid - pos["entry_price"]) * pos["count_fp"]
                msg = f"{ticker}: entry ${pos['entry_price']:.2f} -> exit ${current_bid:.2f}, gain +{gain_pct:.1f}% (${profit:.2f})"
                send_discord(DISCORD_WEBHOOK_BETS, _POSITION_CLOSED_PREFIX + msg)
                del positions[ticker]
                save_open_positions(positions)
            except Exception as e:
                send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"failed to close {ticker}: {e}")


def process_league_real_trading(client, league, seen_trades, sharpapi_rows):
    series_ticker = LEAGUE_SERIES[league]
    kalshi_markets = get_open_markets(client, series_ticker)
    kalshi_events = group_kalshi_markets_by_event(kalshi_markets)

    from datetime import timezone

    edges = find_moneyline_edges(sharpapi_rows)

    # Only trade games starting soon (within NEAR_TERM_HOURS) -- filters
    # out anything days away.
    NEAR_TERM_HOURS = float(os.getenv("NEAR_TERM_HOURS", "36"))
    now = datetime.now(timezone.utc)
    near_term_edges = []
    for e in edges:
        start_str = e.get("event_start_time")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception:
            continue
        hours_until = (start_dt - now).total_seconds() / 3600
        if 0 <= hours_until <= NEAR_TERM_HOURS:
            near_term_edges.append(e)

    edges = near_term_edges
    edges_by_event = defaultdict(list)
    for e in edges:
        edges_by_event[e["event_id"]].append(e)

    for event_id, event_edges in edges_by_event.items():
        away_team, home_team = event_edges[0]["away_team"], event_edges[0]["home_team"]
        match_map = safe_match_event(kalshi_events, away_team, home_team)
        if not match_map:
            continue

        for edge in event_edges:
            trade_key = f"{league}:{edge['event_id']}:{edge['selection']}"
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
                f"[{league.upper()}] {edge['selection']}\n"
                f"Matchup: {edge['away_team']} @ {edge['home_team']}\n"
                f"Kalshi ticker: {match.ticker}\n"
                f"Kalshi price: ${kalshi_price:.2f}  Fair: {edge['fair_prob']*100:.1f}%  Edge: +{edge_pct:.2f}%"
            )
            count_fp = max(1.0, MAX_STAKE_PER_TRADE / kalshi_price)
            matchup_str = f"{edge['away_team']} @ {edge['home_team']}"
            if execute_kalshi_buy(client, match.ticker, kalshi_price, count_fp, msg,
                                   fair_prob=edge['fair_prob'], edge_pct=edge_pct,
                                   matchup=matchup_str, league=league):
                seen_trades.add(trade_key)
                save_seen_trades(seen_trades)


def check_daily_summary():
    today = date.today().isoformat()
    last = {}
    if os.path.exists(LAST_SUMMARY_FILE):
        with open(LAST_SUMMARY_FILE) as f:
            last = json.load(f)

    if last.get("date") == today:
        return

    summary = pt.get_paper_trade_summary()
    lines = ["Daily paper-trading report, sir:\n"]

    any_resolved = False
    for category, stats in summary.items():
        if stats["resolved"] == 0:
            lines.append(f"{category.upper()}: {stats['total_picks']} picks made, none resolved yet.")
            continue
        any_resolved = True
        pnl_str = f"${stats['total_hypothetical_pnl']:+.2f} over {stats['pnl_sample_size']} priced picks" if stats["total_hypothetical_pnl"] is not None else "no price data captured"
        lines.append(
            f"{category.upper()}: {stats['wins']}/{stats['resolved']} correct "
            f"({stats['win_rate']:.1f}%), hypothetical P&L: {pnl_str}."
        )

    if any_resolved:
        profitable = [c for c, s in summary.items() if s["resolved"] >= 15 and (s["total_hypothetical_pnl"] or -999) > 0]
        if profitable:
            lines.append(f"\n{', '.join(profitable).upper()} showing real hypothetical profit over a real sample. Worth considering going live, sir.")
        else:
            lines.append("\nNothing showing genuine hypothetical profit yet — recommend continuing to paper trade.")

    send_discord(DISCORD_WEBHOOK_UPDATES, "\n".join(lines))
    with open(LAST_SUMMARY_FILE, "w") as f:
        json.dump({"date": today}, f)


def run_once(client, seen_trades):
    check_and_close_profitable_positions(client)

    for league in LEAGUE_SERIES.keys():
        try:
            print(f"[{league}] fetching odds...")
            rows = fetch_sharpapi_odds(league)
            print(f"[{league}] got {len(rows)} odds rows")
            process_league_real_trading(client, league, seen_trades, rows)

            kalshi_markets = get_open_markets(client, LEAGUE_SERIES[league])
            kalshi_events = group_kalshi_markets_by_event(kalshi_markets)
            pt.make_moneyline_paper_picks(league, rows, kalshi_events, safe_match_event, send_discord, DISCORD_WEBHOOK_UPDATES)
        except Exception as e:
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"[{league}] scan error: {e}")

    try:
        print("[btc] checking momentum...")
        pt.make_btc_paper_pick(client, MarketStatus, send_discord, DISCORD_WEBHOOK_UPDATES)
        pt.resolve_btc_paper_trades(client, send_discord, DISCORD_WEBHOOK_UPDATES)
        pt.resolve_moneyline_paper_trades(client, send_discord, DISCORD_WEBHOOK_UPDATES)
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"BTC paper trading error: {e}")

    try:
        check_daily_summary()
    except Exception as e:
        print(f"[summary] error: {e}")


def main():
    print("--- AR894 Autonomous Worker (real: NFL+NCAAF moneyline | paper: consensus picks + BTC momentum) ---")
    seen_trades = load_seen_trades()
    client = KalshiClient()

    send_discord(DISCORD_WEBHOOK_UPDATES, "Systems online, sir. I'll only speak up when there's something worth saying.")
    while True:
        try:
            run_once(client, seen_trades)
        except Exception as e:
            print(f"[loop] error: {e}")
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + str(e))
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
