import os
import time
import tempfile
import threading
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
# Optional, separate channel -- falls back to DISCORD_WEBHOOK_UPDATES if
# not set, so nothing breaks until this is actually configured in Railway.
# Set DISCORD_WEBHOOK_ERRORS to a different channel's webhook URL to split
# error traffic out.
DISCORD_WEBHOOK_ERRORS = os.getenv("DISCORD_WEBHOOK_ERRORS", DISCORD_WEBHOOK_UPDATES)

PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "20.0"))
MIN_EDGE_PCT = 2.0

import math

def kalshi_taker_fee_dollars(price_dollars, contracts=1.0, multiplier=1.0):
    """
    Kalshi's real taker-order fee, confirmed live from their published fee
    schedule (kalshi.com/docs/kalshi-fee-schedule.pdf, July 2026 update):
    fee = round_up(0.07 * M * C * P * (1-P)). Every order this bot places
    is a taker order (crosses the spread to fill immediately), so this is
    the real cost paid on every entry -- worst near a 50c coin-flip price
    (up to ~1.75c/contract), smaller near the extremes.

    This was NEVER subtracted anywhere before (2026-09-08) -- not from the
    edge threshold that decides whether a trade is worth taking, not even
    from the retrospective "would this have been profitable" P&L number.
    Confirmed live: on real entry prices this bot actually used today
    (0.47-0.61 range), the fee alone was 2.3%-3.7% of the stake -- equal to
    or bigger than the entire 2-3% edge threshold being used to justify
    the trade in the first place. That's very likely the real reason a
    58-100%-"accurate" strategy still wasn't showing real profit.
    """
    raw = 0.07 * multiplier * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(raw * 10000) / 10000.0  # round up to the nearest hundredth of a cent


# Minimum extra edge (in the same "cents per $1-face-value contract" units
# as edge_pct) required ABOVE the fee before a trade is worth taking --
# i.e. edge_pct must clear kalshi_taker_fee_dollars(price)*100 by at least
# this much, not just clear the raw MIN_EDGE_PCT bar.
# Keeps a real (if modest) expected profit margin after the real cost of
# trading, instead of a threshold that the fee alone can already consume.
MIN_NET_EDGE_AFTER_FEE_PCT = float(os.getenv("MIN_NET_EDGE_AFTER_FEE_PCT", "1.0"))


def clears_fee_adjusted_edge(edge_pct, price_dollars, min_edge_pct):
    """
    True if edge_pct clears BOTH the existing raw threshold AND leaves at
    least MIN_NET_EDGE_AFTER_FEE_PCT of edge remaining after Kalshi's real
    taker fee at this price. Centralizes the fee-adjusted check so every
    call site (real trading AND paper trading) evaluates
    "is this actually worth it" the same way.
    """
    if edge_pct < min_edge_pct:
        return False
    fee_pct = kalshi_taker_fee_dollars(price_dollars) * 100
    return (edge_pct - fee_pct) >= MIN_NET_EDGE_AFTER_FEE_PCT
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
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
# Sports (SharpAPI) scanning cadence. NOTE (2026-09-10): the account
# checked earlier (sharpapi.com, 60 req/min) turned out to be a DIFFERENT
# service from api.sharpapi.io, which is what this bot actually calls --
# that account's real tier/limit is still unconfirmed (its docs list
# Free=12/min, Hobby=120/min, Pro=300/min). Left at the original safe
# 300s cadence until the real api.sharpapi.io account is checked.
# Position closes/reconciliation and paper-trade resolution still run
# every SCAN_INTERVAL_SECONDS regardless of this.
SPORTS_SCAN_INTERVAL_SECONDS = int(os.getenv("SPORTS_SCAN_INTERVAL_SECONDS", "300"))

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1/odds"

# Confirmed live on the real api.sharpapi.io account (2026-09-10, Free
# plan): 12 requests/minute, and that account had already hit the limit
# 2,402 times in the prior 7 days -- the scan loop below fires ~11 calls
# back-to-back with no spacing, so it was blowing the whole per-minute
# budget in the first couple leagues and then eating 429/retry waits for
# the rest of the cycle. That reactive retry-after-429 pattern is slower
# than just not bursting in the first place, so every SharpAPI call now
# goes through this shared pacer first: it keeps calls at least
# _SHARPAPI_MIN_GAP_SECONDS apart, comfortably under the 12/min cap
# (60/12 = 5.0s min; 5.5s leaves a small safety margin), so a full scan
# should now mostly avoid 429s instead of paying for them after the fact.
_SHARPAPI_MIN_GAP_SECONDS = 5.5
_last_sharpapi_call_at = 0.0


def _pace_sharpapi_call():
    global _last_sharpapi_call_at
    now = time.time()
    wait = _SHARPAPI_MIN_GAP_SECONDS - (now - _last_sharpapi_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_sharpapi_call_at = time.time()

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
    # "atp": "KXATPMATCH",  # dropped 2026-09-09 at the user's request --
    # tennis (best-of-3/5, momentum swings mid-match) judged too volatile
    # for this strategy. Existing ATP paper trades stay in the historical
    # data; this just stops any new ones from being made.
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
    # Added 2026-09-09 at the user's request, ahead of the season starting,
    # so paper data collection begins on day one instead of catching up
    # late. KXNBAGAME confirmed live and real on Kalshi's own site
    # (kalshi.com/markets/kxnbagame showed a real Boston vs Detroit market)
    # -- SharpAPI's "nba" league key was NOT independently verified (no way
    # to check without burning a real API call), so same safe-fail note as
    # WNBA applies: if SharpAPI doesn't recognize "nba", this league just
    # quietly produces zero rows/picks rather than erroring.
    "nba": "KXNBAGAME",
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

# Sports where Kalshi's short title is an individual's SURNAME, not a full
# team name -- these need surname matching instead of exact-full-name
# matching (which is correct for team sports but would match nobody here).
INDIVIDUAL_ATHLETE_LEAGUES = {"ufc", "atp"}


_EDGE_FOUND_PREFIX = "I've found something, sir. "
_TRADE_EXECUTED_PREFIX = "Done. Order placed: "
_POSITION_CLOSED_PREFIX = "Took the profit while it was there. "
_ERROR_PREFIX = "Small hiccup, sir — nothing to worry about, but you should know: "


ERROR_LOG_FILE = os.path.join(DATA_DIR, "error_log.json")
ERROR_LOG_MAX_ENTRIES = 100


def _record_error_log(message):
    """Every message that starts with _ERROR_PREFIX also gets saved here,
    so the dashboard can show recent errors without anyone having to go
    dig through Discord scrollback. Never raises."""
    try:
        log = safe_read_json(ERROR_LOG_FILE, [])
        log.append({"at": datetime.now().isoformat(), "message": message})
        log = log[-ERROR_LOG_MAX_ENTRIES:]
        atomic_write_json(ERROR_LOG_FILE, log)
    except Exception as e:
        print(f"[error_log] failed to record: {e}")


# Picks used to fire one Discord message the instant each one happened,
# so a scan cycle that found five picks back-to-back showed up as five
# separate messages stacked on top of each other. Instead, non-error
# messages are queued here and sent as a single grouped "slip" once per
# cycle (see flush_discord_queue, called at the end of run_once/main's
# loop) -- one clean message instead of a burst. Errors still go out
# immediately, unbatched, since those need eyes on them right away.
_discord_queue = defaultdict(list)
_discord_queue_lock = threading.Lock()

# Betting picks/trades (DISCORD_WEBHOOK_BETS) send as soon as they're
# flushed -- one combined message per cycle if several fire in the same
# cycle, but never HELD to wait for a bigger batch (2026-09-10, at the
# user's explicit request: no delay on bet notifications, they want to
# know right away). Status/error/resolution noise on
# DISCORD_WEBHOOK_UPDATES still gets a real minimum gap between sends,
# independent of scan cadence, so THAT channel doesn't spam every cycle.
DISCORD_BETS_SLIP_MIN_INTERVAL_SECONDS = int(os.getenv("DISCORD_BETS_SLIP_MIN_INTERVAL_SECONDS", "0"))
DISCORD_UPDATES_SLIP_MIN_INTERVAL_SECONDS = int(os.getenv("DISCORD_UPDATES_SLIP_MIN_INTERVAL_SECONDS", "900"))
_last_flush_at = defaultdict(float)  # webhook_url -> time.monotonic() of last actual send


def _slip_min_interval(webhook_url):
    if webhook_url == DISCORD_WEBHOOK_BETS:
        return DISCORD_BETS_SLIP_MIN_INTERVAL_SECONDS
    return DISCORD_UPDATES_SLIP_MIN_INTERVAL_SECONDS


def send_discord(webhook_url, message, _retries=3, immediate=False):
    if message.startswith(_ERROR_PREFIX):
        _record_error_log(message)
        webhook_url = DISCORD_WEBHOOK_ERRORS
        immediate = True  # errors are never batched -- always sent right away

    if not immediate:
        with _discord_queue_lock:
            _discord_queue[webhook_url].append(message)
        return

    _send_discord_now(webhook_url, message, _retries=_retries)


def _send_discord_now(webhook_url, message, _retries=3):
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


def flush_discord_queue(force=False):
    """Sends whatever's queued as one grouped, slip-style message per
    webhook (numbered list, bet-slip style) -- betting picks flush every
    cycle with no added delay; status/error updates get a real minimum
    gap (see _slip_min_interval), regardless of
    how often this gets called (once per scan cycle). A webhook not yet
    due just keeps its messages queued for the next call that IS due --
    nothing is lost, only delayed and grouped further, which is the
    actual fix for messages reading as too close together. A webhook
    with only one message when it does flush sends it as-is, unchanged
    from before. Splits into multiple sends if the combined text would
    exceed Discord's 2000-char message limit. Pass force=True to flush
    right now regardless of timing (used for a manual "run now" trigger,
    where the person is actively waiting on the result)."""
    now = time.monotonic()
    with _discord_queue_lock:
        due = {}
        for url, msgs in list(_discord_queue.items()):
            if not msgs:
                continue
            if force or (now - _last_flush_at[url]) >= _slip_min_interval(url):
                due[url] = msgs
                _discord_queue[url] = []
                _last_flush_at[url] = now

    for webhook_url, messages in due.items():
        if len(messages) == 1:
            _send_discord_now(webhook_url, messages[0])
            continue

        header = f"**📋 {len(messages)} updates:**\n"
        chunk = header
        for i, m in enumerate(messages, 1):
            line = f"`{i}.` {m}\n"
            if len(chunk) + len(line) > 1900 and chunk != header:
                _send_discord_now(webhook_url, chunk)
                chunk = header
            chunk += line
        if chunk != header:
            _send_discord_now(webhook_url, chunk)


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


SHARPAPI_FETCH_HEALTH_FILE = os.path.join(DATA_DIR, "sharpapi_fetch_health.json")
SHARPAPI_FETCH_HEALTH_MAX_ENTRIES = 200


def _record_fetch_health(league, pages_fetched, total_rows, complete, reason=None, kind="odds"):
    """
    Every fetch_sharpapi_odds (and, as of 2026-09-11, fetch_sharpapi_
    player_props) call logs one entry here -- not just to console (which
    nobody reads unless something's already gone wrong), but to a small
    persisted history so "is the bot's OWN unassisted cycle silently
    getting incomplete data" can be answered by reading real history
    instead of guessing or triggering more fetches. When a fetch comes
    back incomplete, also fires exactly one Discord alert (not one per
    retry) so this can't go unnoticed.

    `kind` distinguishes odds vs props entries -- props fetches used to
    be entirely invisible here, which hid the fact that they (not odds)
    were the bulk of a scan cycle's real wall-clock time.
    """
    try:
        history = safe_read_json(SHARPAPI_FETCH_HEALTH_FILE, [])
        entry = {
            "league": league, "kind": kind, "at": datetime.now(timezone.utc).isoformat(),
            "pages_fetched": pages_fetched, "total_rows": total_rows,
            "complete": complete, "reason": reason,
        }
        history.append(entry)
        history = history[-SHARPAPI_FETCH_HEALTH_MAX_ENTRIES:]
        atomic_write_json(SHARPAPI_FETCH_HEALTH_FILE, history)
        if not complete:
            send_discord(
                DISCORD_WEBHOOK_UPDATES,
                f"\u26a0\ufe0f [sharpapi] {league} fetch came back INCOMPLETE this cycle "
                f"({pages_fetched} pages, {total_rows} rows) -- {reason}. Some games may be "
                f"missing from this cycle's picks; should self-correct on the next 5-min cycle."
            )
    except Exception as e:
        print(f"[sharpapi] fetch health logging failed (non-fatal): {e}")


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

    Every incomplete outcome (rate-limit exhaustion, missing cursor, hit
    the page cap) is now recorded via _record_fetch_health instead of
    only printed -- confirmed live (2026-09-08) that manual diagnostic
    calls competing for the same 12/req-min budget can truncate a fetch
    silently; this makes that visible instead of guessable.
    """
    all_rows = []
    cursor = None
    restarted_after_cursor_expiry = False
    pages_fetched = 0
    for page_num in range(SHARPAPI_MAX_PAGES):
        params = {"league": league, "market": "main", "limit": 200}
        if cursor:
            params["cursor"] = cursor

        for attempt in range(4):
            _pace_sharpapi_call()
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
            _record_fetch_health(league, pages_fetched, len(all_rows), complete=False, reason="rate-limited after 4 retries")
            return all_rows

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
                pages_fetched = 0
                cursor = None
                continue
            print(f"[sharpapi] {league} failed: {resp.status_code} {resp.text[:200]}")
            _record_fetch_health(league, pages_fetched, len(all_rows), complete=False, reason=f"HTTP {resp.status_code}")
            return all_rows

        pages_fetched += 1
        body = resp.json()
        all_rows.extend(body.get("data", []))
        pagination = body.get("pagination", {})
        if not pagination.get("has_more"):
            _record_fetch_health(league, pages_fetched, len(all_rows), complete=True)
            return all_rows
        cursor = pagination.get("next_cursor")
        if not cursor:
            print(f"[sharpapi] {league} WARNING: has_more=true but no next_cursor returned -- stopping early, some games may be missing")
            _record_fetch_health(league, pages_fetched, len(all_rows), complete=False, reason="has_more=true but no next_cursor")
            return all_rows
    else:
        print(f"[sharpapi] {league} WARNING: hit the {SHARPAPI_MAX_PAGES}-page safety cap -- there may be even more games than that")
        _record_fetch_health(league, pages_fetched, len(all_rows), complete=False, reason=f"hit {SHARPAPI_MAX_PAGES}-page safety cap")
    return all_rows


def _write_props_debug_sample(league, sample_row, note):
    try:
        from state_io import atomic_write_json, safe_read_json
        path = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "props_debug_sample.json")
        data = safe_read_json(path, {})
        data[league] = {"sample_row": sample_row, "note": note, "at": datetime.now().isoformat()}
        atomic_write_json(path, data)
    except Exception as e:
        print(f"[props] debug sample write failed: {e}")


PROP_MARKET_PROBE_CANDIDATES = ["player_prop", "props", "player_points", "player_props"]


def probe_sharpapi_player_prop_market(league="mlb"):
    """
    ONE-TIME diagnostic (2026-09-10): web-fetched SharpAPI docs gave
    CONTRADICTORY answers for the player-prop market parameter and field
    names across two separate lookups (one said "player_prop" +
    "selection", another said "props"/"player_points" + "selection_type")
    -- that contradiction means the doc summaries can't be trusted blind.
    This tries each realistic candidate against the REAL API with a
    small limit, and writes the raw HTTP status + response body for each
    to a debug file (readable at /debug/props_probe) so the actual
    working value can be confirmed from real data instead of guessed at
    again. Runs once (guarded by the debug file already existing) so it
    doesn't burn extra rate-limited requests every cycle.
    """
    from state_io import atomic_write_json, safe_read_json
    probe_path = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "props_market_probe.json")
    if safe_read_json(probe_path, None) is not None:
        return  # already probed
    results = {}
    for market_value in PROP_MARKET_PROBE_CANDIDATES:
        try:
            resp = requests.get(
                SHARPAPI_BASE,
                params={"league": league, "market": market_value, "limit": 5},
                headers={"X-API-Key": SHARPAPI_KEY},
                timeout=15,
            )
            body_text = resp.text[:1500]
            try:
                body_json = resp.json()
                row_count = len(body_json.get("data", []))
                sample = body_json.get("data", [])[0] if row_count else None
            except Exception:
                row_count = None
                sample = None
            results[market_value] = {
                "status_code": resp.status_code, "row_count": row_count,
                "sample_row": sample, "raw_body_truncated": body_text,
            }
        except Exception as e:
            results[market_value] = {"error": str(e)}
        time.sleep(1.5)  # stay well under the 12/min rate limit across 4 probes
    atomic_write_json(probe_path, {"league": league, "at": datetime.now().isoformat(), "results": results})


# Separate, much lower page cap for player-props pagination. Props
# fetches were silently reusing SHARPAPI_MAX_PAGES (10 pages, up to
# 2000 rows) per league with no visibility -- confirmed live
# (2026-09-11) that full scan cycles were taking 13-15 minutes end to
# end, vs. an intended ~5 minutes, and the *logged* (odds-only)
# fetches only accounted for a few minutes of that -- the rest was
# unlogged props pagination eating the same paced request budget. A
# single day's player-prop lines don't need anywhere near 2000 rows
# per league; 3 pages (600 rows) comfortably covers a full slate
# while cutting the worst-case props pagination time by more than half.
SHARPAPI_PROPS_MAX_PAGES = int(os.getenv("SHARPAPI_PROPS_MAX_PAGES", "3"))


def fetch_sharpapi_player_props(league):
    """
    Player-prop odds for one league from SharpAPI, mirroring
    fetch_sharpapi_odds's pagination/rate-limit handling (see that
    function's docstring for the details this reuses).

    HONESTY UP FRONT (2026-09-09): SharpAPI's marketing site confirms a
    "Player Props" market type exists, but the exact JSON field names for
    it aren't in SharpAPI's public docs, and there's no way to test this
    live from this machine (no local copy of the API key -- it only lives
    in Railway's environment). The row shape parsed here
    (paper_trading._parse_player_prop_row) is a best-effort guess at the
    likely field names. If it's wrong, this safely returns rows that
    _parse_player_prop_row can't parse (never a crash, never a guessed
    value) -- the first successful raw row gets printed to the logs and
    sent to Discord so a real fix can follow, instead of silently doing
    nothing forever.
    """
    all_rows = []
    cursor = None
    logged_sample = False
    for page_num in range(SHARPAPI_PROPS_MAX_PAGES):
        params = {"league": league, "market": "props", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = None
        for attempt in range(4):
            _pace_sharpapi_call()
            resp = requests.get(SHARPAPI_BASE, params=params, headers={"X-API-Key": SHARPAPI_KEY})
            if resp.status_code != 429:
                break
            time.sleep(3)
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "no response"
            print(f"[sharpapi] {league} player_props failed: {code}")
            _record_fetch_health(league, page_num, len(all_rows), complete=False, reason=f"HTTP {code}", kind="props")
            return all_rows
        body = resp.json()
        rows = body.get("data", [])
        if rows and not logged_sample:
            print(f"[sharpapi] {league} player_props sample row (for schema verification): {rows[0]}")
            logged_sample = True
            send_discord(
                DISCORD_WEBHOOK_UPDATES,
                f"[props] {league} player_props first sample row, for verifying the field names match what the code expects:\n{rows[0]}",
            )
            # Also written to a small per-league debug file (readable via
            # the dashboard's /debug/props_sample route) so this can be
            # checked directly without needing Discord access -- keyed by
            # league so MLB/WNBA/NBA don't overwrite each other.
            _write_props_debug_sample(league, rows[0], None)
        elif not rows and not logged_sample:
            # Confirms the request itself didn't error but came back empty --
            # different from a schema-mismatch (which would still return rows,
            # just ones _parse_player_prop_row can't read).
            _write_props_debug_sample(league, None, "request succeeded but returned zero rows")
        all_rows.extend(rows)
        pagination = body.get("pagination", {})
        if not pagination.get("has_more"):
            _record_fetch_health(league, page_num + 1, len(all_rows), complete=True, kind="props")
            return all_rows
        cursor = pagination.get("next_cursor")
        if not cursor:
            _record_fetch_health(league, page_num + 1, len(all_rows), complete=False, reason="has_more=true but no next_cursor", kind="props")
            return all_rows
    _record_fetch_health(league, SHARPAPI_PROPS_MAX_PAGES, len(all_rows), complete=False, reason=f"hit {SHARPAPI_PROPS_MAX_PAGES}-page props safety cap", kind="props")
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


NAME_SUFFIXES = {"JR", "JR.", "SR", "SR.", "II", "III", "IV", "V"}


def surname(full_name):
    """Last word of a full name, normalized -- e.g. 'Alexandre Pantoja' -> 'PANTOJA'.
    Strips trailing generational suffixes (Jr/Sr/II/III/IV/V) first -- otherwise
    a fighter like 'Sean King III' would surname-match as 'III' instead of
    'KING', which silently breaks if one data source includes the suffix and
    the other doesn't (confirmed as a live risk on the 2026-09-12 UFC card,
    which has exactly this fighter)."""
    parts = (full_name or "").strip().split()
    while parts and parts[-1].upper().rstrip(".") in {s.rstrip(".") for s in NAME_SUFFIXES}:
        parts = parts[:-1]
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
    capped at 100% of budget. Pass edge_pct=None for a strategy that is always
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
                entry_fee = kalshi_taker_fee_dollars(pos["entry_price"], pos["count_fp"])
                exit_fee = kalshi_taker_fee_dollars(current_bid, pos["count_fp"])
                profit = (current_bid - pos["entry_price"]) * pos["count_fp"] - entry_fee - exit_fee
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
        entry_fee = kalshi_taker_fee_dollars(entry_price, count_fp)
        pnl = ((1.0 - entry_price) * count_fp if won else -entry_price * count_fp) - entry_fee

        new_total = ledger.record_bot_trade_result(ticker, pnl, note=f"{side_label} settled {result.upper()}")

        send_discord(
            DISCORD_WEBHOOK_UPDATES,
            f"[SETTLED] {ticker} [{side_label}]: {'WON' if won else 'LOST'} "
            f"(P&L: ${pnl:+.2f}, bot lifetime P&L: ${new_total:+.2f})"
        )
        ledger.check_loss_limit_circuit_breaker(send_discord, DISCORD_WEBHOOK_BETS)

        del positions[ticker]
        save_open_positions(positions)


MANUAL_POSITIONS_FILE = os.path.join(DATA_DIR, "manual_positions.json")

def load_manual_positions():
    return safe_read_json(MANUAL_POSITIONS_FILE, {})

def save_manual_positions(positions):
    atomic_write_json(MANUAL_POSITIONS_FILE, positions)


def sync_manual_positions(client):
    """
    Auto-detects and logs bets the user places THEMSELVES directly on
    Kalshi (not through this bot), by diffing the real account's live
    positions against the bot's own known open positions (OPEN_POSITIONS_FILE)
    -- anything left over is something the user must have placed manually,
    since it's the same Kalshi account either way. Replaces having to fill
    out a manual-bet form by hand: this runs every cycle, same as
    reconcile_settled_positions.

    While a manual position is open, this keeps its tracked entry price and
    size updated to the account's current average (in case the user adds to
    it). Once a tracked manual position disappears from the live list, it's
    been closed one of two ways:
      - it settled (won/lost) -- get_settlements() gives the exact real
        revenue vs. cost, so the recorded P&L is exact, not estimated.
      - the user sold it back before settlement -- there's no settlement
        record for that, so P&L is best-effort from the fill history
        (buys cost money, sells return money, fees always subtract).
    Either way it's recorded to manual_positions.json with a real dollar
    P&L, and the dashboard can show it without the user typing anything in.
    """
    try:
        live_positions = client.portfolio.get_positions()
    except Exception as e:
        print(f"[manual_positions] could not fetch live positions: {e}")
        return

    live_map = {}
    for p in live_positions:
        count_fp = float(getattr(p, "position_fp", 0) or 0)
        if count_fp == 0:
            continue
        live_map[p.ticker] = p

    bot_tickers = set(load_open_positions().keys())
    store = load_manual_positions()

    # --- detect new / update open manual positions ---
    for ticker, p in live_map.items():
        if ticker in bot_tickers:
            continue  # this is the bot's own real position, not a manual one

        count_fp = float(p.position_fp)
        side_label = "YES" if count_fp > 0 else "NO"
        exposure = float(getattr(p, "market_exposure_dollars", 0) or 0)
        entry_price = round(exposure / abs(count_fp), 4) if count_fp else None

        rec = store.get(ticker)
        if rec is None or rec.get("status") != "open":
            market_title = None
            try:
                market_title = getattr(client.get_market(ticker), "title", None)
            except Exception:
                pass
            store[ticker] = {
                "ticker": ticker,
                "side": side_label,
                "count_fp": abs(count_fp),
                "entry_price": entry_price,
                "market_title": market_title,
                "opened_at": datetime.now().isoformat(),
                "status": "open",
            }
            save_manual_positions(store)
            send_discord(
                DISCORD_WEBHOOK_UPDATES,
                f"[MANUAL BET DETECTED] {ticker}"
                + (f" ({market_title})" if market_title else "")
                + f" [{side_label}] x{abs(count_fp):.2f} @ ~${entry_price:.2f} "
                  "-- looks like you placed this yourself on Kalshi, now tracking it.",
            )
        else:
            # still open -- keep size/avg-price current in case they added to it
            rec["count_fp"] = abs(count_fp)
            if entry_price is not None:
                rec["entry_price"] = entry_price

    # --- resolve manual positions that dropped off the live list ---
    dirty = False
    for ticker, rec in list(store.items()):
        if rec.get("status") != "open" or ticker in live_map:
            continue

        pnl = None
        note = None
        try:
            settlements = client.portfolio.get_settlements(ticker=ticker, fetch_all=True)
        except Exception:
            settlements = []
        settle = next((s for s in settlements if getattr(s, "ticker", None) == ticker), None)

        if settle is not None:
            cost = float(getattr(settle, "yes_total_cost_dollars", None) or 0) +                    float(getattr(settle, "no_total_cost_dollars", None) or 0)
            revenue = (getattr(settle, "revenue", 0) or 0) / 100.0
            pnl = round(revenue - cost, 2)
            note = f"settled {getattr(settle, 'market_result', None) or '?'}"
        else:
            # Not in settlements -- most likely sold back manually before the
            # market settled. Reconstruct P&L from the fill history: buys
            # (book_side == bid) cost money, sells (book_side == ask) return
            # money, fees always subtract. Best-effort -- if fills can't be
            # read this is left as pnl_unknown rather than guessed.
            try:
                fills = client.portfolio.get_fills(ticker=ticker, fetch_all=True)
                net = 0.0
                for f in fills:
                    price = float(f.yes_price_dollars or f.no_price_dollars or 0)
                    qty = float(f.count_fp or 0)
                    fee = float(getattr(f, "fee_cost_dollars", None) or 0)
                    net += (-price * qty if f.is_bid else price * qty) - fee
                pnl = round(net, 2)
                note = "closed early (from fill history)"
            except Exception as e:
                note = f"closed, could not reconstruct P&L ({e})"

        rec["status"] = "resolved"
        rec["resolved_at"] = datetime.now().isoformat()
        rec["pnl"] = pnl
        rec["note"] = note
        dirty = True

        if pnl is not None:
            send_discord(
                DISCORD_WEBHOOK_UPDATES,
                f"[MANUAL BET RESOLVED] {ticker}: {note}, P&L ${pnl:+.2f}",
            )
        else:
            send_discord(
                DISCORD_WEBHOOK_UPDATES,
                f"[MANUAL BET CLOSED] {ticker}: {note} -- check Kalshi directly for exact P&L",
            )

    if dirty:
        save_manual_positions(store)


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
                if clears_fee_adjusted_edge(yes_edge_pct, yes_price, MIN_EDGE_PCT) and edge["fair_prob"] >= favorite_min_prob:
                    side_to_trade = Side.YES
                    trade_price = yes_price
                    trade_edge_pct = yes_edge_pct
                    trade_fair_prob = edge["fair_prob"]

            if side_to_trade is None and no_ask:
                no_price = float(no_ask)
                fair_prob_no = 1 - edge["fair_prob"]
                no_edge_pct = (fair_prob_no - no_price) * 100
                if clears_fee_adjusted_edge(no_edge_pct, no_price, MIN_EDGE_PCT) and fair_prob_no >= favorite_min_prob:
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


CYCLE_STATUS_FILE = os.path.join(DATA_DIR, "cycle_status.json")


def _record_cycle_status(phase, run_sports_scan=None, error=None):
    """Tracks what the fast/main loop is doing right now, purely for the
    dashboard's 'what's it doing / when's the next scan' panel. Never
    raises, never affects trading logic."""
    try:
        status = safe_read_json(CYCLE_STATUS_FILE, {})
        now_iso = datetime.now().isoformat()
        if phase == "started":
            status["cycle_started_at"] = now_iso
            status["run_sports_scan"] = run_sports_scan
            status["last_error"] = None
        elif phase == "finished":
            status["cycle_finished_at"] = now_iso
            status["scan_interval_seconds"] = SCAN_INTERVAL_SECONDS
        elif phase == "error":
            status["last_error"] = {"at": now_iso, "message": str(error)}
        atomic_write_json(CYCLE_STATUS_FILE, status)
    except Exception as e:
        print(f"[cycle_status] failed to record: {e}")


def run_once(client, seen_trades, run_sports_scan=True):
    _record_cycle_status("started", run_sports_scan=run_sports_scan)
    check_and_close_profitable_positions(client)
    reconcile_settled_positions(client)
    sync_manual_positions(client)

    if not run_sports_scan:
        run_fast_cycle(client)
        _record_cycle_status("finished")
        return

    # Real trading only for REAL_TRADING_LEAGUES; every league still gets
    # paper-traded (unlimited volume, full logging) regardless.
    #
    # MAX DATA COLLECTION MODE for paper moneyline (2026-09-09, at the
    # user's explicit request to speed up data-gathering since real
    # trading is 100% off anyway -- zero real-money cost either way).
    # favorite_min_prob=0.50 means it takes literally every game where
    # both sides have real 2-way odds and a Kalshi match (whichever side
    # is even slightly favored, no matter how close), and min_edge_pct=0
    # means it no longer requires a detected mispricing edge either.
    # This does NOT touch get_favorite_min_prob() / MIN_EDGE_PCT above,
    # which is what REAL trading uses -- if real trading is ever turned
    # on for a league, it still requires an actual edge, unaffected by
    # this. Toggle back to a narrower net later with
    # AGGRESSIVE_PAPER_DATA_COLLECTION=false if this turns out to be too
    # noisy to learn from (e.g. coin-flip games swamping real signal).
    if os.getenv("AGGRESSIVE_PAPER_DATA_COLLECTION", "true").lower() == "true":
        paper_favorite_min_prob = 0.50
        paper_min_edge_pct = 0.0
    else:
        paper_favorite_min_prob = max(0.50, pt.get_effective_favorite_min_prob() - 0.03)
        paper_min_edge_pct = 1.5
    # Collected across every league this cycle, then handed to the parlay
    # builder once the loop finishes -- see paper_trading.maybe_make_parlay_pick.
    try:
        probe_sharpapi_player_prop_market("mlb")
    except Exception as e:
        print(f"[props] market probe error: {e}")

    cycle_new_picks = []

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
            new_picks = pt.make_moneyline_paper_picks(
                league, rows, kalshi_events, safe_match_event, send_discord, DISCORD_WEBHOOK_BETS,
                min_edge_pct=paper_min_edge_pct, favorite_min_prob=paper_favorite_min_prob,
                # So each pick also gets flagged "manual_bet_candidate" -- whether
                # it ALSO clears the tighter bar real trading uses, i.e. whether
                # this is a pick worth placing yourself, not just data collection.
                real_min_edge_pct=MIN_EDGE_PCT, real_favorite_min_prob=get_favorite_min_prob(),
            )
            if new_picks:
                cycle_new_picks.extend(new_picks)
            live_trading.track_live_candidates(league, rows, kalshi_events, safe_match_event)  # LIVE TRADING HOOK -- delete this line to remove the feature

            # PrizePicks-style player prop picks -- only for leagues we can
            # actually grade automatically (see paper_trading.py's prop
            # section docstring).
            if league in pt.PROP_GRADABLE_LEAGUES:
                prop_rows = fetch_sharpapi_player_props(league)
                pt.maybe_make_prop_pick(league, prop_rows, send_discord, DISCORD_WEBHOOK_BETS)
                # Passing yards -- MAIN FOCUS pick type at the user's request,
                # high volume, independent picks (see paper_trading.py).
                if league in pt.PASSING_YARDS_LEAGUES:
                    pt.maybe_make_passing_yards_picks(league, prop_rows, send_discord, DISCORD_WEBHOOK_BETS)
                # WNBA combined-stat picks (points+rebounds+assists-style
                # combos) -- second MAIN FOCUS pick type at the user's
                # request, same high-volume/independently-graded approach
                # as passing yards above, just for WNBA's combo props.
                if league in pt.WNBA_COMBINED_STATS_LEAGUES:
                    pt.maybe_make_wnba_combined_picks(league, prop_rows, send_discord, DISCORD_WEBHOOK_BETS)
        except Exception as e:
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"[{league}] scan error: {e}")

    try:
        pt.maybe_make_parlay_pick(cycle_new_picks, send_discord, DISCORD_WEBHOOK_BETS)
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"parlay builder error: {e}")

    run_fast_cycle(client)
    _record_cycle_status("finished")


def run_fast_cycle(client):
    """Everything that should run on the FAST cadence (SCAN_INTERVAL_SECONDS)
    regardless of whether this cycle also did a full sports scan -- live-game
    tracking and paper-trade resolution, both of which move faster than
    sports games' own SPORTS_SCAN_INTERVAL_SECONDS. (BTC used to run here
    too; removed 2026-09-10 at the user's request -- too volatile to be
    worth learning from.)"""
    try:
        live_trading.monitor_live_games(client, send_discord, None)  # LIVE TRADING HOOK -- delete this line to remove the feature; Discord notice turned off 2026-09-10 at user's request (keeping only bet-slip + tie notifications)
        live_trading.check_tie_alerts(client, send_discord, DISCORD_WEBHOOK_UPDATES)  # TIE ALERT HOOK -- delete this line to remove the feature
        pt.track_moneyline_contract_prices(client)  # MONEYLINE PRICE HISTORY HOOK -- feeds the early-exit check below
        pt.check_and_close_moneyline_paper_early(send_discord, None)  # MONEYLINE EARLY-EXIT HOOK -- paper-only profit-take; Discord notice off 2026-09-10
        pt.resolve_moneyline_paper_trades(client, send_discord, None)  # Discord notice off 2026-09-10 at user's request
        pt.resolve_parlay_paper_trades(send_discord, None)  # Discord notice off 2026-09-10 at user's request
        pt.resolve_prop_paper_trades(send_discord, None)  # Discord notice off 2026-09-10 at user's request
        pt.resolve_passing_yards_picks(send_discord, None)  # Discord notice off 2026-09-10, consistent with the other resolve calls above -- new picks still post
        pt.resolve_wnba_combined_picks(send_discord, None)  # same pattern as passing yards resolution above
        pt.check_profitability_milestones(
            send_discord, DISCORD_WEBHOOK_UPDATES,
            real_trading_on_by_category={"moneyline": bool(REAL_TRADING_LEAGUES)},
        )  # tells the user once a strategy crosses real sample + real profit, keeps reminding daily until turned on
    except Exception as e:
        send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + f"fast-cycle error: {e}")

    try:
        ledger.check_for_new_deposit(client, send_discord, None, "https://ar894-engine-production.up.railway.app")  # Discord notice off 2026-09-10 at user's request
    except Exception as e:
        print(f"[ledger] deposit check error: {e}")

    try:
        pt.maybe_adjust_moneyline_favorite_threshold(send_discord, None)  # Discord notice off 2026-09-10 at user's request
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
    real_status = "NONE (paused, 100% paper)" if not REAL_TRADING_LEAGUES else f"{sorted(REAL_TRADING_LEAGUES)}"
    print(f"--- Picks Autonomous Worker (real trading: {real_status} | paper: all leagues, learning-gated) ---")
    start_dashboard_thread()
    seen_trades = load_seen_trades()
    client = KalshiClient()

    send_discord(DISCORD_WEBHOOK_UPDATES, "Updated and back online, sir.", immediate=True)

    last_sports_scan = 0.0
    while True:
        run_sports_scan = (time.time() - last_sports_scan) >= SPORTS_SCAN_INTERVAL_SECONDS
        # Stamped at the START of a sports scan, not after it finishes.
        # Confirmed live (2026-09-11): a full sports scan itself takes a
        # few minutes (SharpAPI rate-limit pacing), and stamping this
        # AFTER run_once returned meant that scan duration and the next
        # SPORTS_SCAN_INTERVAL_SECONDS wait stacked back-to-back instead
        # of overlapping -- observed cycle-to-cycle gaps of ~9 minutes
        # against a 5-minute SPORTS_SCAN_INTERVAL_SECONDS setting.
        # Stamping at the start makes the interval clock run WHILE the
        # scan itself is running, so the next scan is due close to
        # SPORTS_SCAN_INTERVAL_SECONDS after this one started, not after
        # it finished.
        if run_sports_scan:
            last_sports_scan = time.time()
        try:
            run_once(client, seen_trades, run_sports_scan=run_sports_scan)
        except Exception as e:
            print(f"[loop] error: {e}")
            _record_cycle_status("error", error=e)
            send_discord(DISCORD_WEBHOOK_UPDATES, _ERROR_PREFIX + str(e))
        finally:
            flush_discord_queue()  # send this cycle's picks as one grouped slip
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
