import os
import time
import tempfile
from datetime import datetime, date
from collections import defaultdict

import requests
from pykalshi import KalshiClient, Action, Side, MarketStatus

import paper_trading as pt
import ledger
from state_io import atomic_write_json, safe_read_json

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

PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "20.0"))
MIN_EDGE_PCT = 2.0
# Only bet on the side that's actually favored to win (its own fair win
# probability must clear this bar), not just wherever a thin statistical
# edge happens to point -- betting AGAINST a favorite for a small edge is
# a coinflip-ish, higher-variance play even when the math is sound. This
# keeps trades to backing favorites, which is more conservative.
#
# This is now a LEARNED value, not a fixed constant: paper_trading's
# maybe_adjust_moneyline_favorite_threshold() raises it once there's real
# evidence that favorites just above the old bar were too close to trust.
# get_favorite_min_prob() always reads the current learned value live, so
# real trading (once a league is enabled here) picks up the same lessons
# paper trading learns. The env var is only the starting point before any
# adjustment has ever happened.
FAVORITE_MIN_PROB = float(os.getenv("FAVORITE_MIN_PROB", "0.55"))


def get_favorite_min_prob():
    return pt.get_effective_favorite_min_prob()
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
# Sports (SharpAPI) scanning stays on its own slower cadence -- games move
# on a much longer clock than BTC's 15-minute windows, and SharpAPI calls
# are the heavier/more rate-limit-sensitive part. BTC checks, position
# closes/reconciliation, and paper-trade resolution still run every
# SCAN_INTERVAL_SECONDS regardless of this.
SPORTS_SCAN_INTERVAL_SECONDS = int(os.getenv("SPORTS_SCAN_INTERVAL_SECONDS", "300"))

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1/odds"
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DAILY_STATE_FILE = os.path.join(DATA_DIR, "daily_trading_state.json")
SEEN_TRADES_FILE = os.path.join(DATA_DIR, "seen_trades.json")
OPEN_POSITIONS_FILE = os.path.join(DATA_DIR, "open_positions.json")
LAST_SUMMARY_FILE = os.path.join(DATA_DIR, "last_summary.json")

LEAGUE_SERIES = {
    "nfl": "KXNFLGAME",
    "ncaaf": "KXNCAAFGAME",
    "mlb": "KXMLBGAME",
    "ufc": "KXUFCFIGHT",
    "atp": "KXATPMATCH",
    # Added while WNBA is still in season (regular season/playoffs run into
    # October) -- confirmed SharpAPI carries "wnba" as a basketball league,
    # and confirmed Kalshi has WNBA game markets. The exact series ticker
    # here (KXWNBAGAME) follows every other team sport's confirmed pattern
    # in this dict (KX{LEAGUE}GAME) but wasn't independently verified live
    # against Kalshi's API before deploying -- if it's wrong, get_open_markets
    # just returns zero markets and this league silently produces no picks
    # (same safe-fail behavior as a real team-name mismatch), so check the
    # Railway logs after deploy for "[wnba] fetching odds..." followed by a
    # nonzero markets count to confirm it's actually finding real markets.
    "wnba": "KXWNBAGAME",
    # "wta": "KXWTAMATCH",  # dropped: SharpAPI/Kalshi cover different WTA
    # tournament tiers right now, zero overlap -- revisit later if that changes
}

# Real-money sports trading is paused entirely -- every league in
# LEAGUE_SERIES still gets paper-traded (unlimited, logs everything) so
# there's a track record to review, but nothing here places real orders
# right now. Move a league in here only once its paper results (and the
# learning model's adjustments) have been reviewed and real trading is
# deliberately turned back on.
REAL_TRADING_LEAGUES = set()

# BTC real-money trading is paused too, for the same reason -- everything
# is 100% paper right now while the learning model builds up a track
# record. Flip back on with BTC_REAL_TRADING_ENABLED=true once ready.
BTC_REAL_TRADING_ENABLED = os.getenv("BTC_REAL_TRADING_ENABLED", "false").lower() == "true"

# Sports where Kalshi's short title is an individual's SURNAME, not a full
# team name -- these need surname matching instead of exact-full-name
# matching (which is correct for team sports but would match nobody here).
INDIVIDUAL_ATHLETE_LEAGUES = {"ufc", "atp"}


_EDGE_FOUND_PREFIX = "I've found something, sir. "
_TRADE_EXECUTED_PREFIX = "Done. Order placed: "
_POSITION_CLOSED_PREFIX = "Took the profit while it was there. "
_ERROR_PREFIX = "Small hiccup, sir — nothing to worry about, but you should know: "


def send_discord(webhook_url, message, _retries=3):
    for attempt in range(_retries):
        try:
            resp = requests.post(webhook_url, json={"content": message}, timeout=5)
            if resp.status_code == 204:
                return
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1)
                time.sleep(retry_after + 0.1)
                continue
            print(f"[discord] non-204: {resp.status_code} {resp.text[:200]}")
            return
        except Exception as e:
            print(f"[discord] send failed: {e}")
            return
    print(f"[discord] gave up after {_retries} rate-limit retries")


def load_daily_state():
    today = date.today().isoformat()
    state = safe_read_json(DAILY_STATE_FILE, None)
    if state and state.get("date") == today:
        return state
    return {"date": today, "realized_loss": 0.0, "trades_executed": 0, "halted": False}


def save_daily_state(state):
    atomic_write_json(DAILY_STATE_FILE, state)


def load_seen_trades():
    return set(safe_read_json(SEEN_TRADES_FILE, []))


def save_seen_trades(seen):
    atomic_write_json(SEEN_TRADES_FILE, list(seen))


def load_open_positions():
    return safe_read_json(OPEN_POSITIONS_FILE, {})


def save_open_positions(positions):
    atomic_write_json(OPEN_POSITIONS_FILE, positions)


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


def surname(full_name):
    """Last word of a full name, normalized -- e.g. 'Alexandre Pantoja' -> 'PANTOJA'."""
    parts = (full_name or "").strip().split()
    return parts[-1].upper() if parts else ""


def safe_match_event(kalshi_events, away_team, home_team, individual=False):
    """
    Matches on EXACT normalized equality only -- team name for team sports,
    surname for individual-athlete sports. Never substring/containment,
    which can silently pair the wrong real-world game or person. If nothing
    matches exactly, we skip the trade instead of guessing.
    """
    if individual:
        away_key, home_key = surname(away_team), surname(home_team)
        get_key = lambda title: surname(short_name(title))
    else:
        away_key, home_key = normalize_team_name(away_team), normalize_team_name(home_team)
        get_key = lambda title: normalize_team_name(short_name(title))

    for event_ticker, markets in kalshi_events.items():
        if len(markets) != 2:
            continue
        m1, m2 = markets
        s1_key, s2_key = get_key(m1.title), get_key(m2.title)

        if s1_key == away_key and s2_key == home_key:
            return {away_team: m1, home_team: m2}
        if s1_key == home_key and s2_key == away_key:
            return {home_team: m1, away_team: m2}

    return None


TRADE_AUDIT_LOG = os.path.join(DATA_DIR, "trade_audit_log.json")

def log_trade_decision(ticker, price_dollars, count_fp, fair_prob, edge_pct, matchup, league, side="YES", event_start_time=None):
    entry = {
        "ticker": ticker, "league": league, "matchup": matchup, "side": side,
        "kalshi_price": price_dollars, "fair_prob": fair_prob, "edge_pct": edge_pct,
        "count_fp": count_fp, "stake": price_dollars * count_fp,
        "event_start_time": event_start_time,  # when the game itself was/is, not when we decided
        "decided_at": datetime.now().isoformat(),
    }
    log = safe_read_json(TRADE_AUDIT_LOG, [])
    log.append(entry)
    atomic_write_json(TRADE_AUDIT_LOG, log)


# Kalshi charges a taker fee on top of the raw stake, and on a cheap/thin
# contract that fee can be a meaningful chunk of the trade -- the naive
# stake-vs-available check doesn't leave room for it, which was enough to
# tip a trade into "insufficient_balance" at the exchange even though our
# own math said it was affordable. This buffer keeps a margin for that.
FEE_SAFETY_BUFFER = 1.15


def compute_stake_dollars(available, edge_pct=None):
    """
    Sports: stake scales UP with how good the edge is -- a bigger edge means
    more confidence, so it bets more. At the minimum qualifying edge
    (MIN_EDGE_PCT), it stakes the base STAKE_PERCENT of available budget;
    every additional multiple of that edge scales the stake up proportionally,
    capped at 100% of budget. Pass edge_pct=None for BTC, which is always
    allowed to use the full available budget (no sports-style edge to scale on).

    Either way, the result is capped just under available/FEE_SAFETY_BUFFER so
    it always leaves enough headroom to pass the pre-trade fee-buffer check --
    otherwise "use the whole budget" would make that check impossible to pass.
    """
    if edge_pct is not None and MIN_EDGE_PCT > 0:
        pct = min(1.0, ledger.STAKE_PERCENT * (edge_pct / MIN_EDGE_PCT))
    else:
        pct = 1.0
    stake = max(available * pct, ledger.STAKE_MIN_DOLLARS)
    headroom_cap = (available / FEE_SAFETY_BUFFER) * 0.99
    return max(min(stake, headroom_cap), ledger.STAKE_MIN_DOLLARS)


def execute_kalshi_buy(client, ticker, price_dollars, count_fp, discord_msg, side=Side.YES, fair_prob=None, edge_pct=None, matchup=None, league=None, seen_trades=None, trade_key=None, event_start_time=None):
    side_label = "YES" if side == Side.YES else "NO"

    state = load_daily_state()

    available = ledger.get_available_budget(client, load_open_positions())
    stake = price_dollars * count_fp
    if stake * FEE_SAFETY_BUFFER > available:
        print(f"Available budget (${available:.2f}) below required stake + fee buffer (${stake * FEE_SAFETY_BUFFER:.2f}) — skipping {ticker}")
        return False

    if fair_prob is not None:
        log_trade_decision(ticker, price_dollars, count_fp, fair_prob, edge_pct, matchup, league, side_label, event_start_time=event_start_time)

    try:
        price_kwargs = (
            {"yes_price_dollars": f"{price_dollars:.4f}"} if side == Side.YES
            else {"no_price_dollars": f"{price_dollars:.4f}"}
        )
        client.portfolio.place_order(
            ticker, Action.BUY, side,
            count_fp=str(round(count_fp, 2)),
            **price_kwargs,
        )
        send_discord(DISCORD_WEBHOOK_BETS, _TRADE_EXECUTED_PREFIX + f"{ticker} [{side_label}] x{count_fp:.2f} @ ${price_dollars:.2f}")
        state["trades_executed"] += 1
        save_daily_state(state)

        positions = load_open_positions()
        positions[ticker] = {
            "entry_price": price_dollars, "count_fp": count_fp,
            "side": side_label, "opened_at": datetime.now().isoformat(),
        }
        save_open_positions(positions)
        return True
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"trade failed for {ticker}: {e}")
        # A real rejection from the exchange (e.g. insufficient_balance) on
        # THIS specific order means retrying the identical thing next cycle
        # is pointless and just spams the same failure repeatedly. Mark it
        # seen so it's not retried again for this market's lifetime.
        if seen_trades is not None and trade_key is not None:
            seen_trades.add(trade_key)
            save_seen_trades(seen_trades)
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

        side_label = pos.get("side", "YES")  # older positions predate NO-side support, default YES
        side = Side.YES if side_label == "YES" else Side.NO
        bid_field = "yes_bid_dollars" if side_label == "YES" else "no_bid_dollars"

        current_bid = getattr(market, bid_field, None)
        if not current_bid:
            continue

        current_bid = float(current_bid)
        gain_pct = ((current_bid - pos["entry_price"]) / pos["entry_price"]) * 100

        if gain_pct >= PROFIT_TARGET_PCT:
            try:
                price_kwargs = (
                    {"yes_price_dollars": f"{current_bid:.4f}"} if side == Side.YES
                    else {"no_price_dollars": f"{current_bid:.4f}"}
                )
                client.portfolio.place_order(
                    ticker, Action.SELL, side,
                    count_fp=str(pos["count_fp"]),
                    **price_kwargs,
                )
                profit = (current_bid - pos["entry_price"]) * pos["count_fp"]
                msg = f"{ticker} [{side_label}]: entry ${pos['entry_price']:.2f} -> exit ${current_bid:.2f}, gain +{gain_pct:.1f}% (${profit:.2f})"
                send_discord(DISCORD_WEBHOOK_BETS, _POSITION_CLOSED_PREFIX + msg)
                del positions[ticker]
                save_open_positions(positions)
            except Exception as e:
                send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"failed to close {ticker}: {e}")


def reconcile_settled_positions(client):
    """
    Catches real positions that settled (won or lost) since we opened them
    -- separate from check_and_close_profitable_positions, which only
    handles taking an early profit. This covers everything else: a losing
    position, or one held all the way to market settlement.

    Records the result into ledger.record_bot_trade_result, which is
    scoped to only trades this bot itself placed -- unlike Kalshi's
    account-wide realized P&L, which also mixes in any manual trading on
    this account.
    """
    positions = load_open_positions()
    if not positions:
        return

    try:
        live_positions = client.portfolio.get_positions()
        live_tickers = {p.ticker for p in live_positions if float(getattr(p, "position_fp", 0) or 0) != 0}
    except Exception as e:
        print(f"[reconcile] could not fetch live positions: {e}")
        return

    for ticker, pos in list(positions.items()):
        if ticker in live_tickers:
            continue  # still open, nothing to reconcile

        try:
            market = client.get_market(ticker)
        except Exception as e:
            print(f"[reconcile] could not fetch market {ticker}: {e}")
            continue

        result = getattr(market, "result", None)
        if result not in ("yes", "no"):
            continue  # not actually settled -- leave it, don't guess

        side_label = pos.get("side", "YES")
        won = (result == side_label.lower())
        entry_price = pos["entry_price"]
        count_fp = pos["count_fp"]
        pnl = (1.0 - entry_price) * count_fp if won else -entry_price * count_fp

        new_total = ledger.record_bot_trade_result(ticker, pnl, note=f"{side_label} settled {result.upper()}")

        send_discord(
            DISCORD_WEBHOOK_UPDATES,
            f"[SETTLED] {ticker} [{side_label}]: {'WON' if won else 'LOST'} "
            f"(P&L: ${pnl:+.2f}, bot lifetime P&L: ${new_total:+.2f})"
        )

        del positions[ticker]
        save_open_positions(positions)


def process_league_real_trading(client, league, seen_trades, sharpapi_rows):
    series_ticker = LEAGUE_SERIES[league]
    kalshi_markets = get_open_markets(client, series_ticker)
    kalshi_events = group_kalshi_markets_by_event(kalshi_markets)

    from datetime import timezone

    edges = find_moneyline_edges(sharpapi_rows)

    # Same-day only -- the event must start later today (UTC), not just within some
    # rolling hour window that could roll into tomorrow.
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
        if hours_until >= 0 and start_dt.date() == now.date():
            near_term_edges.append(e)

    edges = near_term_edges
    edges_by_event = defaultdict(list)
    for e in edges:
        edges_by_event[e["event_id"]].append(e)

    for event_id, event_edges in edges_by_event.items():
        away_team, home_team = event_edges[0]["away_team"], event_edges[0]["home_team"]
        match_map = safe_match_event(kalshi_events, away_team, home_team, individual=(league in INDIVIDUAL_ATHLETE_LEAGUES))
        if not match_map:
            continue

        for edge in event_edges:
            trade_key = f"{league}:{edge['event_id']}:{edge['selection']}"
            if trade_key in seen_trades:
                continue
            match = match_map.get(edge["selection"])
            if not match:
                continue

            try:
                existing_positions = client.portfolio.get_positions()
                held_tickers = {
                    p.ticker for p in existing_positions
                    if float(getattr(p, "position_fp", 0) or 0) != 0
                }
                if match.ticker in held_tickers:
                    seen_trades.add(trade_key)
                    continue
            except Exception as e:
                print(f"[safety] could not verify existing positions, skipping trade to be safe: {e}")
                continue

            # Check BOTH directions: buy YES if this team looks undervalued,
            # or buy NO ("short" the team) if it looks overvalued. Only one
            # side can genuinely be an edge for a given team -- check YES
            # first, fall back to NO only if YES doesn't clear the bar.
            yes_ask = getattr(match, "yes_ask_dollars", None)
            no_ask = getattr(match, "no_ask_dollars", None)

            side_to_trade = None
            trade_price = None
            trade_edge_pct = None
            trade_fair_prob = None

            favorite_min_prob = get_favorite_min_prob()
            if yes_ask:
                yes_price = float(yes_ask)
                yes_edge_pct = (edge["fair_prob"] - yes_price) * 100
                if yes_edge_pct >= MIN_EDGE_PCT and edge["fair_prob"] >= favorite_min_prob:
                    side_to_trade = Side.YES
                    trade_price = yes_price
                    trade_edge_pct = yes_edge_pct
                    trade_fair_prob = edge["fair_prob"]

            if side_to_trade is None and no_ask:
                no_price = float(no_ask)
                fair_prob_no = 1 - edge["fair_prob"]
                no_edge_pct = (fair_prob_no - no_price) * 100
                if no_edge_pct >= MIN_EDGE_PCT and fair_prob_no >= favorite_min_prob:
                    side_to_trade = Side.NO
                    trade_price = no_price
                    trade_edge_pct = no_edge_pct
                    trade_fair_prob = fair_prob_no

            if side_to_trade is None:
                continue

            side_label = "YES" if side_to_trade == Side.YES else "NO"
            msg = (
                f"[{league.upper()}] {edge['selection']} ({side_label})\n"
                f"Matchup: {edge['away_team']} @ {edge['home_team']}\n"
                f"Kalshi ticker: {match.ticker}\n"
                f"Kalshi price: ${trade_price:.2f}  Fair: {trade_fair_prob*100:.1f}%  Edge: +{trade_edge_pct:.2f}%"
            )
            available = ledger.get_available_budget(client, load_open_positions())
            stake_dollars = compute_stake_dollars(available, edge_pct=trade_edge_pct)
            count_fp = max(1.0, stake_dollars / trade_price)
            matchup_str = f"{edge['away_team']} @ {edge['home_team']}"
            if execute_kalshi_buy(client, match.ticker, trade_price, count_fp, msg, side=side_to_trade,
                                   fair_prob=trade_fair_prob, edge_pct=trade_edge_pct,
                                   matchup=matchup_str, league=league,
                                   seen_trades=seen_trades, trade_key=trade_key,
                                   event_start_time=edge.get("event_start_time")):
                seen_trades.add(trade_key)
                save_seen_trades(seen_trades)


def process_btc_real_trading(client):
    """Real-money BTC trading using the same momentum signal as the paper
    experiment, gated by live ledger budget (separate smaller stake size
    than sports, given the higher uncertainty of this method)."""
    price = pt.get_btc_spot_price()
    if price is None:
        return
    history = pt.load_btc_price_history()
    window = pt.get_effective_btc_momentum_window()  # same learned window paper trading uses
    if len(history) < window:
        return
    momentum = history[-1]["price"] - history[-window]["price"]
    direction = "up" if momentum > 0 else "down"

    try:
        markets = client.get_markets(series_ticker="KXBTC15M", status=MarketStatus.OPEN, limit=5)
    except Exception as e:
        print(f"[btc-real] market fetch failed: {e}")
        return
    if not markets:
        return
    market = sorted(markets, key=lambda m: getattr(m, "close_time", None) or "9999")[0]

    trade_key = f"btc_real:{market.ticker}"
    seen = load_seen_trades()
    if trade_key in seen:
        return

    side = Side.YES if direction == "up" else Side.NO
    price_field = "yes_ask_dollars" if direction == "up" else "no_ask_dollars"
    ask = getattr(market, price_field, None)
    if not ask:
        return
    ask_price = float(ask)

    # Same price-discipline gate paper trading uses (see
    # paper_trading.get_btc_fair_prob_estimate) -- confirmed live this
    # strategy can win most of its bets and still lose money if it pays
    # whatever price is offered, so real trading needs this check too,
    # not just the paper simulation.
    fair_prob_estimate = pt.get_btc_fair_prob_estimate()
    if fair_prob_estimate is not None:
        edge_pct = (fair_prob_estimate - ask_price) * 100
        if edge_pct < pt.BTC_MIN_EDGE_PCT:
            return

    available = ledger.get_available_budget(client, load_open_positions())
    stake_dollars = compute_stake_dollars(available)  # BTC may use the full available budget
    count_fp = max(1.0, stake_dollars / ask_price)

    msg = f"[BTC] {direction.upper()} momentum signal\nMarket: {market.title}\nPrice: ${ask_price:.2f}"
    if execute_kalshi_buy(client, market.ticker, ask_price, count_fp, msg, side=side, league="btc", matchup=market.title,
                           seen_trades=seen, trade_key=trade_key):
        seen.add(trade_key)
        save_seen_trades(seen)


def check_daily_summary():
    today = date.today().isoformat()
    last = safe_read_json(LAST_SUMMARY_FILE, {})

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
    atomic_write_json(LAST_SUMMARY_FILE, {"date": today})


def run_once(client, seen_trades, run_sports_scan=True):
    check_and_close_profitable_positions(client)
    reconcile_settled_positions(client)

    if not run_sports_scan:
        run_btc_and_resolution(client)
        return

    # Real trading only for REAL_TRADING_LEAGUES; every league still gets
    # paper-traded (unlimited volume, full logging) regardless.
    for league in LEAGUE_SERIES.keys():
        try:
            print(f"[{league}] fetching odds...")
            rows = fetch_sharpapi_odds(league)
            print(f"[{league}] got {len(rows)} odds rows")

            if league in REAL_TRADING_LEAGUES:
                process_league_real_trading(client, league, seen_trades, rows)

            kalshi_markets = get_open_markets(client, LEAGUE_SERIES[league])
            kalshi_events = group_kalshi_markets_by_event(kalshi_markets)
            pt.make_moneyline_paper_picks(league, rows, kalshi_events, safe_match_event, send_discord, DISCORD_WEBHOOK_UPDATES)
        except Exception as e:
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"[{league}] scan error: {e}")

    run_btc_and_resolution(client)


def run_btc_and_resolution(client):
    """Everything that should run on the FAST cadence (SCAN_INTERVAL_SECONDS)
    regardless of whether this cycle also did a full sports scan: BTC's own
    15-minute windows move much faster than sports games do, so this stays
    decoupled from SPORTS_SCAN_INTERVAL_SECONDS."""
    try:
        print("[btc] checking momentum...")
        if BTC_REAL_TRADING_ENABLED:
            process_btc_real_trading(client)
        pt.make_btc_paper_pick(client, MarketStatus, send_discord, DISCORD_WEBHOOK_UPDATES)
        pt.resolve_btc_paper_trades(client, send_discord, DISCORD_WEBHOOK_UPDATES)
        pt.resolve_moneyline_paper_trades(client, send_discord, DISCORD_WEBHOOK_UPDATES)
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"BTC trading error: {e}")

    try:
        ledger.check_for_new_deposit(client, send_discord, DISCORD_WEBHOOK_UPDATES, "https://ar894-engine-production.up.railway.app")
    except Exception as e:
        print(f"[ledger] deposit check error: {e}")

    try:
        pt.maybe_adjust_btc_momentum_window(send_discord, DISCORD_WEBHOOK_UPDATES)
    except Exception as e:
        print(f"[adjust] error: {e}")

    try:
        pt.maybe_adjust_moneyline_favorite_threshold(send_discord, DISCORD_WEBHOOK_UPDATES)
    except Exception as e:
        print(f"[adjust] moneyline threshold error: {e}")


def start_dashboard_thread():
    import threading
    def run_dashboard():
        try:
            from dashboard import app
            port = int(os.getenv("PORT", 8080))
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            print(f"[dashboard] failed to start: {e}")
    t = threading.Thread(target=run_dashboard, daemon=True)
    t.start()


def main():
    real_status = "NONE (paused, 100% paper)" if not REAL_TRADING_LEAGUES and not BTC_REAL_TRADING_ENABLED else f"{sorted(REAL_TRADING_LEAGUES)}{' + BTC' if BTC_REAL_TRADING_ENABLED else ''}"
    print(f"--- AR894 Autonomous Worker (real trading: {real_status} | paper: all leagues + BTC momentum, learning-gated) ---")
    start_dashboard_thread()
    seen_trades = load_seen_trades()
    client = KalshiClient()

    send_discord(DISCORD_WEBHOOK_UPDATES, "Updated and back online, sir.")

    last_sports_scan = 0.0
    while True:
        run_sports_scan = (time.time() - last_sports_scan) >= SPORTS_SCAN_INTERVAL_SECONDS
        try:
            run_once(client, seen_trades, run_sports_scan=run_sports_scan)
            if run_sports_scan:
                last_sports_scan = time.time()
        except Exception as e:
            print(f"[loop] error: {e}")
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + str(e))
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
