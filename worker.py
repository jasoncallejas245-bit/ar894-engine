import os
import time
import tempfile
from datetime import datetime, date, timezone
from collections import defaultdict

import requests
from pykalshi import KalshiClient, Action, Side, MarketStatus

import paper_trading as pt
import live_trading  # LIVE TRADING HOOK -- delete this import to remove the feature
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

# Optional hard cap on a single real BTC stake, in dollars -- unset by
# default, which preserves the existing "use the full available budget"
# behavior exactly as before. Set this (e.g. BTC_MAX_STAKE_DOLLARS=1.00)
# to run a deliberately small, bounded real-money test without touching
# the approved allowance itself. Safe to leave set permanently too, if
# a smaller-than-full-budget stake size is ever wanted long-term.
_btc_max_stake_env = os.getenv("BTC_MAX_STAKE_DOLLARS")
BTC_MAX_STAKE_DOLLARS = float(_btc_max_stake_env) if _btc_max_stake_env else None

# Kalshi split BTC/crypto markets onto their own "exchange shard" (shard
# index 2) on 2026-08-24 -- collateral has to be pre-allocated on that
# specific shard before an order can land there, separate from the
# account's overall balance. Confirmed live: this account's balance was
# 100% sitting on the default shard (0) with $0 on the crypto shard,
# which would silently reject every real BTC order regardless of
# strategy correctness. BTC_SHARD_INDEX / DEFAULT_SHARD_INDEX name the
# two sides of that transfer; the actual top-up happens in
# ensure_crypto_shard_funded() below, called right before any real BTC
# order is placed.
BTC_SHARD_INDEX = 2
DEFAULT_SHARD_INDEX = 0
# Keep a little extra buffer on the crypto shard beyond exactly what one
# trade needs, so back-to-back trades don't re-trigger a transfer every
# single cycle.
SHARD_TRANSFER_BUFFER_DOLLARS = 2.0
# Leave at least this much on the default shard -- never sweep it to $0.
SHARD_MIN_RESERVE_DOLLARS = 1.0
# Don't attempt more than one shard transfer this often, so a persistent
# error (or Kalshi-side processing delay) can't turn into a transfer-spam
# loop -- transfers are also documented as processed asynchronously, so
# this gives one time to land before trying again.
SHARD_TRANSFER_COOLDOWN_SECONDS = 300
SHARD_TRANSFER_STATE_FILE = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "shard_transfer_state.json")


def ensure_crypto_shard_funded(client, needed_dollars):
    """
    Confirmed live (real $1 transfer, 2026-09-07) that Kalshi's Intra
    Account Transfer endpoint works via client.post(path, data=body) --
    pykalshi's post() doesn't take a `json` kwarg, but plain `data` (form-
    encoded) is accepted and Kalshi processes it correctly. This checks
    whether the crypto shard already holds enough for the trade about to
    be placed, and tops it up from the default shard if not.

    Returns True if the crypto shard already has (or now has) enough to
    proceed. Returns False if funding isn't possible right now (not
    enough on the default shard, in cooldown after a recent attempt, or
    the balance/transfer call itself failed) -- callers should skip
    placing the order this cycle rather than risk an order rejected for
    an insufficient-shard-balance reason that has nothing to do with the
    trading signal itself. Never raises.
    """
    try:
        balance = client.get("/portfolio/balance")
        breakdown = {row["exchange_index"]: float(row["balance"]) for row in balance.get("balance_breakdown", [])}
    except Exception as e:
        print(f"[shard-fund] balance check failed: {e}")
        return False

    crypto_balance = breakdown.get(BTC_SHARD_INDEX, 0.0)
    if crypto_balance >= needed_dollars:
        return True

    state = safe_read_json(SHARD_TRANSFER_STATE_FILE, {})
    last_attempt = state.get("last_attempt")
    if last_attempt:
        try:
            last_dt = datetime.fromisoformat(last_attempt)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < SHARD_TRANSFER_COOLDOWN_SECONDS:
                return False  # recently tried -- give a pending transfer time to land
        except Exception:
            pass

    default_balance = breakdown.get(DEFAULT_SHARD_INDEX, 0.0)
    available_to_move = default_balance - SHARD_MIN_RESERVE_DOLLARS
    shortfall = (needed_dollars + SHARD_TRANSFER_BUFFER_DOLLARS) - crypto_balance
    transfer_amount = min(shortfall, available_to_move)

    atomic_write_json(SHARD_TRANSFER_STATE_FILE, {"last_attempt": datetime.now(timezone.utc).isoformat()})

    if transfer_amount <= 0:
        print(f"[shard-fund] can't fund crypto shard -- default shard only has ${default_balance:.2f} "
              f"(need to move ${shortfall:.2f}, min reserve ${SHARD_MIN_RESERVE_DOLLARS:.2f})")
        return False

    body = {
        "source": "event_contract",
        "destination": "event_contract",
        "amount": round(transfer_amount * 10000),  # Kalshi wants centicents for this endpoint
        "source_exchange_shard": DEFAULT_SHARD_INDEX,
        "destination_exchange_shard": BTC_SHARD_INDEX,
    }
    try:
        resp = client.post("/portfolio/intra_exchange_instance_transfer", data=body)
        print(f"[shard-fund] moved ${transfer_amount:.2f} from shard {DEFAULT_SHARD_INDEX} to "
              f"shard {BTC_SHARD_INDEX} (transfer_id={resp.get('transfer_id')})")
    except Exception as e:
        print(f"[shard-fund] transfer failed: {e}")
    return False  # transfer is async -- skip this cycle's order either way, try again next cycle

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


# CONFIRMED (SharpAPI docs, docs.sharpapi.io/en/api-reference/overview/):
# /api/v1/odds hard-caps every response at 200 rows per page REGARDLESS of
# what "limit" is requested -- an earlier fix here that just raised the
# limit param was a no-op for exactly that reason. The response is a flat
# list of (event, selection, sportsbook) rows, not one row per game, so a
# single busy day can easily produce more than 200 rows total (2 selections
# x N sportsbooks x M games) -- meaning games sorted past the cutoff were
# being silently dropped with nothing in the logs to show it. This is the
# real, confirmed cause of "the bot didn't bet on a real game that was
# happening" -- not a probability/edge filter being too strict, but some
# games never even making it into the data the filters saw.
#
# Real fix: /odds supports cursor-based pagination (`pagination.next_cursor`
# in the response, fed back as the `cursor` request param) specifically so
# multi-page scans over live/changing odds don't drift -- looping until
# `pagination.has_more` is false actually gets everything instead of
# guessing at a big-enough single-page limit.
SHARPAPI_MAX_PAGES = 10  # safety cap -- 10 x 200 = 2000 rows is far beyond any single day's slate for one league


def fetch_sharpapi_odds(league):
    """
    Paginates through SharpAPI's /odds endpoint until has_more is false.

    Discovered live (2026-09-07, right after pagination was first added):
    fetching multiple pages for one big league (NFL/NCAAF/MLB routinely
    need 4-5 pages now that they're not silently truncated at 200 rows)
    can burn through SharpAPI's per-minute rate limit (confirmed live:
    "limit":12 requests/min) before the LATER leagues in the same scan
    cycle (UFC/ATP/WNBA) even get their first request in -- they came back
    429'd with 0 rows, a new failure mode this pagination fix introduced.
    Retrying 429s with the API's own retry-after/reset_at (same pattern
    already used for Discord's rate limit in send_discord) fixes that:
    each league's fetch now waits out the limit instead of giving up.
    """
    all_rows = []
    cursor = None
    restarted_after_cursor_expiry = False
    for page_num in range(SHARPAPI_MAX_PAGES):
        params = {"league": league, "market": "main", "limit": 200}
        if cursor:
            params["cursor"] = cursor

        for attempt in range(4):
            resp = requests.get(SHARPAPI_BASE, params=params, headers={"X-API-Key": SHARPAPI_KEY})
            if resp.status_code != 429:
                break
            try:
                err = resp.json().get("error", {})
                reset_at = err.get("reset_at")
                wait_s = (datetime.fromisoformat(reset_at.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() if reset_at else 5
            except Exception:
                wait_s = 5
            wait_s = max(0.5, min(wait_s, 30)) + 0.5  # sane floor/ceiling plus a small safety margin
            print(f"[sharpapi] {league} rate-limited (page {page_num + 1}), waiting {wait_s:.1f}s")
            time.sleep(wait_s)
        else:
            print(f"[sharpapi] {league} still rate-limited after retries, stopping with what we have")
            break

        if resp.status_code != 200:
            # Confirmed live: a long enough 429 wait (rate-limited page 5,
            # waited 30+s across a redeploy) let the cursor's underlying
            # "store generation" rotate server-side, so resuming with the
            # OLD cursor came back as a 400 cursor_expired -- not a 429,
            # a different failure the retry loop above doesn't catch. The
            # API's own error message says the fix: drop the cursor and
            # restart from page 1. Only do this once per fetch (not every
            # page) so a persistently-failing league can't loop forever.
            try:
                is_cursor_expired = resp.json().get("error", {}).get("code") == "cursor_expired"
            except Exception:
                is_cursor_expired = False
            if is_cursor_expired and not restarted_after_cursor_expiry:
                print(f"[sharpapi] {league} cursor expired mid-fetch -- restarting pagination from page 1")
                restarted_after_cursor_expiry = True
                all_rows = []
                cursor = None
                continue
            print(f"[sharpapi] {league} failed: {resp.status_code} {resp.text[:200]}")
            break

        body = resp.json()
        all_rows.extend(body.get("data", []))
        pagination = body.get("pagination", {})
        if not pagination.get("has_more"):
            break
        cursor = pagination.get("next_cursor")
        if not cursor:
            print(f"[sharpapi] {league} WARNING: has_more=true but no next_cursor returned -- stopping early, some games may be missing")
            break
    else:
        print(f"[sharpapi] {league} WARNING: hit the {SHARPAPI_MAX_PAGES}-page safety cap -- there may be even more games than that")
    return all_rows


def _longest_names_row(rows):
    """
    Picks whichever row (from any iterable of sportsbook rows) has the
    longest combined away_team + home_team text -- a cheap, reliable way
    to prefer the unabbreviated spelling (e.g. FanDuel's "Boston Red
    Sox") over an abbreviated one (e.g. DraftKings' "BOS Red Sox")
    without hardcoding a per-team abbreviation table. Matters because
    away_team/home_team feed directly into Kalshi matching, which needs
    the real spelling.
    """
    return max(rows, key=lambda r: len(r.get("away_team") or "") + len(r.get("home_team") or ""))


def find_moneyline_edges(rows):
    grouped = defaultdict(dict)
    for row in rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        side = pt._selection_side(row.get("selection"), row.get("away_team"), row.get("home_team"))
        if side is None:
            continue  # can't tell which team this selection refers to -- skip rather than guess
        key = (row.get("event_id"), side)
        grouped[key][row.get("sportsbook")] = row

    by_event = defaultdict(set)
    for (event_id, side) in grouped.keys():
        by_event[event_id].add(side)

    edges = []
    for event_id, sides in by_event.items():
        sides = list(sides)
        if len(sides) != 2:
            continue
        side_a, side_b = sides
        rows_a, rows_b = grouped[(event_id, side_a)], grouped[(event_id, side_b)]
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

        # One row, from either side, whichever book has the longest combined
        # away+home text -- keeps away_team/home_team consistent (both from
        # the same book) rather than potentially mixing an abbreviated field
        # from one book with a full-name field from another.
        best_row = _longest_names_row(list(rows_a.values()) + list(rows_b.values()))
        away_team, home_team = best_row.get("away_team"), best_row.get("home_team")
        team_a = away_team if side_a == "away" else home_team
        team_b = away_team if side_b == "away" else home_team

        for selection, fair_prob in [(team_a, fair_a), (team_b, fair_b)]:
            edges.append({
                "event_id": event_id,
                "away_team": away_team,
                "home_team": home_team,
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


def reduced_name_candidates(normalized_full):
    """
    Kalshi sometimes titles an MLB market by city alone ('Cleveland wins'),
    or -- when two teams share a city, like the LA Angels/Dodgers, NY
    Mets/Yankees, or Chicago Cubs/White Sox -- by city plus the initials
    of the mascot's word(s) ('Los Angeles A', 'Los Angeles D', 'Chicago
    WS'). Confirmed live via /debug_kalshi_match. Generates every
    "leading words + initials of the remaining word(s)" reduction of a
    full team name so those can be checked for an EXACT match against
    Kalshi's title, without hardcoding a city/mascot table per team --
    still exact-equality only, never substring/containment, so it can't
    silently pair the wrong game.
    """
    words = normalized_full.split()
    candidates = {normalized_full}
    for split in range(1, len(words)):
        prefix = " ".join(words[:split])
        candidates.add(prefix)  # mascot fully dropped -- e.g. "Cleveland Guardians" -> "Cleveland"
        initials = "".join(w[0] for w in words[split:] if w)
        candidates.add(f"{prefix} {initials}".strip())  # shared-city disambiguation -- see docstring
    return candidates


def safe_match_event(kalshi_events, away_team, home_team, individual=False):
    """
    Matches on EXACT normalized equality only -- team name (or one of its
    known reductions -- see reduced_name_candidates) for team sports,
    surname for individual-athlete sports. Never substring/containment,
    which can silently pair the wrong real-world game or person. If nothing
    matches exactly, we skip the trade instead of guessing.
    """
    if individual:
        away_candidates, home_candidates = {surname(away_team)}, {surname(home_team)}
        get_key = lambda title: surname(short_name(title))
    else:
        away_candidates = reduced_name_candidates(normalize_team_name(away_team))
        home_candidates = reduced_name_candidates(normalize_team_name(home_team))
        get_key = lambda title: normalize_team_name(short_name(title))

    for event_ticker, markets in kalshi_events.items():
        if len(markets) != 2:
            continue
        m1, m2 = markets
        s1_key, s2_key = get_key(m1.title), get_key(m2.title)

        if s1_key in away_candidates and s2_key in home_candidates:
            return {away_team: m1, home_team: m2}
        if s1_key in home_candidates and s2_key in away_candidates:
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
                # This early-exit P&L was never being recorded to the bot's
                # own ledger before -- fixed, since the drawdown circuit
                # breaker below needs an accurate lifetime P&L to work off.
                ledger.record_bot_trade_result(ticker, profit, note=f"{side_label} early-profit exit")
                ledger.check_loss_limit_circuit_breaker(send_discord, DISCORD_WEBHOOK_BETS)
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
        ledger.check_loss_limit_circuit_breaker(send_discord, DISCORD_WEBHOOK_BETS)

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
    if BTC_MAX_STAKE_DOLLARS is not None:
        stake_dollars = min(stake_dollars, BTC_MAX_STAKE_DOLLARS)
    count_fp = max(1.0, stake_dollars / ask_price)

    # SHARD FUNDING HOOK -- delete this block to remove the feature (see
    # ensure_crypto_shard_funded above). BTC markets live on their own
    # Kalshi exchange shard as of 2026-08-24, and money has to be
    # pre-allocated there before an order can land -- this tops the
    # crypto shard up from the default shard whenever it's running low,
    # so real BTC trades don't silently reject for a funding/routing
    # reason that has nothing to do with the trading signal. If a top-up
    # was just initiated (transfers are async), this skips placing the
    # order THIS cycle and tries again once the transfer's had time to land.
    if not ensure_crypto_shard_funded(client, stake_dollars):
        return

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
            distinct_games = len({r.get("event_id") for r in rows if r.get("event_id")})
            print(f"[{league}] got {len(rows)} odds rows across {distinct_games} distinct games")

            if league in REAL_TRADING_LEAGUES and not ledger.is_trading_halted():
                process_league_real_trading(client, league, seen_trades, rows)

            kalshi_markets = get_open_markets(client, LEAGUE_SERIES[league])
            kalshi_events = group_kalshi_markets_by_event(kalshi_markets)
            pt.make_moneyline_paper_picks(league, rows, kalshi_events, safe_match_event, send_discord, DISCORD_WEBHOOK_UPDATES)
            live_trading.track_live_candidates(league, rows, kalshi_events, safe_match_event)  # LIVE TRADING HOOK -- delete this line to remove the feature
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
        if BTC_REAL_TRADING_ENABLED and not ledger.is_trading_halted():
            process_btc_real_trading(client)
        pt.make_btc_paper_pick(client, MarketStatus, send_discord, DISCORD_WEBHOOK_UPDATES)
        pt.track_btc_contract_prices(client)  # BTC PRICE HISTORY HOOK -- delete this line to stop collecting early-exit data
        live_trading.monitor_live_games(client, send_discord, DISCORD_WEBHOOK_UPDATES)  # LIVE TRADING HOOK -- delete this line to remove the feature
        live_trading.check_tie_alerts(send_discord, DISCORD_WEBHOOK_BETS)  # TIE ALERT HOOK -- delete this line to remove the feature
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
