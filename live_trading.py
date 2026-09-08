"""
Live/in-game moneyline paper trading -- EXPERIMENTAL, isolated by design.

Unlike the pregame moneyline strategy (which compares SharpAPI's no-vig
fair price against Kalshi's price to find a genuine mispricing), this
watches ONLY Kalshi's own live price movement on games already in
progress -- no external odds API needed, so it isn't subject to
SharpAPI's rate limits at all. This is the same "trade off the
exchange's own price action" idea the BTC strategy already uses,
applied to sports.

Explicitly designed around a real risk the account owner raised directly:
in-game sports prices swing constantly and reverse constantly (a team
down early coming back is completely normal, not a signal) -- a naive
"price moved, bet that direction" strategy would get whipsawed hard,
buying the "losing" side right before a comeback. So this requires a
SUSTAINED move that HOLDS across multiple spaced-out checks, not a
single price tick, and once it decides on a game it NEVER reconsiders
that game again -- no flip-flopping as the score swings back and forth.
Self-tested against exactly that scenario (a price drop followed by a
recovery) before shipping -- it correctly produces no pick.

100% paper trading, same as every other strategy here. This does not
touch real money and isn't wired into real trading anywhere.

TO FULLY REMOVE THIS FEATURE: delete this file, remove the two lines
that import/call it in worker.py (each marked "LIVE TRADING HOOK"), and
delete live_game_tracker.json from the persistent volume. Nothing else
in the codebase depends on it -- paper_trading.py's moneyline bankroll
and resolution logic are reused as-is (live picks are tagged
source="live" in the same paper_trades.json list), so removing this
file does not touch or corrupt any existing pregame paper-trading data.
"""
import os
from datetime import datetime, timezone

from state_io import atomic_write_json, safe_read_json
import paper_trading as pt

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
TRACKER_FILE = os.path.join(DATA_DIR, "live_game_tracker.json")

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "true").lower() == "true"

# How long after kickoff to keep watching a game before assuming it's over
# and dropping it -- generous enough for long games (MLB extra innings,
# NFL overtime) without tracking stale entries forever.
MAX_GAME_AGE_HOURS = 5.0

# A single price tick means nothing in live sports. This requires the
# price to have moved by at least this much AND held -- not reverted --
# across this many separate checks (spaced by the fast ~1-2 min scan
# interval) before it's treated as a real signal instead of in-game noise.
MOVE_THRESHOLD_CENTS = 8
REQUIRED_CONSISTENT_CHECKS = 3

# Bound how much price history one game keeps, so this can't grow forever
# across a long game.
MAX_HISTORY_PER_GAME = 20


def _load():
    return safe_read_json(TRACKER_FILE, {"games": {}})


def _save(data):
    atomic_write_json(TRACKER_FILE, data)


def track_live_candidates(league, sharpapi_rows, kalshi_events, safe_match_fn):
    """
    Called every pregame sports scan, reusing the SAME rows/kalshi_events
    already fetched for the pregame strategy -- no extra API calls just
    to discover games. Starts watching any game whose scheduled start
    time has already passed (i.e. it's presumably being played right
    now) that isn't already being tracked. Never raises.
    """
    if not LIVE_TRADING_ENABLED:
        return
    try:
        data = _load()
        now = datetime.now(timezone.utc)

        seen_events = {}
        for row in sharpapi_rows:
            if row.get("market_type") != "moneyline" or row.get("is_main_line") is not True:
                continue
            event_id = row.get("event_id")
            seen_events.setdefault(event_id, row)

        changed = False
        for event_id, row in seen_events.items():
            key = f"{league}:{event_id}"
            if key in data["games"]:
                continue  # already tracking, or already decided/dropped

            start_str = row.get("event_start_time")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                continue

            age_hours = (now - start_dt).total_seconds() / 3600
            if not (0 <= age_hours <= MAX_GAME_AGE_HOURS):
                continue  # not started yet, or presumed already over

            away_team, home_team = row.get("away_team"), row.get("home_team")
            match_map = safe_match_fn(kalshi_events, away_team, home_team, individual=(league in {"ufc", "atp"}))
            if not match_map:
                continue

            away_market = match_map.get(away_team)
            home_market = match_map.get(home_team)
            if not away_market or not home_market:
                continue

            data["games"][key] = {
                "league": league,
                "event_id": event_id,
                "away_team": away_team,
                "home_team": home_team,
                "away_ticker": away_market.ticker,
                "home_ticker": home_market.ticker,
                "start_time": start_str,
                "first_seen_at": now.isoformat(),
                "price_history": [],
                "decided": False,
            }
            changed = True

        if changed:
            _save(data)
    except Exception as e:
        print(f"[live_trading] track_live_candidates error ({league}): {e}")


def _sustained_direction(history, threshold, required_checks):
    """
    Returns ("away"|"home", opening_price, current_price) if the last
    `required_checks` snapshots ALL show that side's price having moved up
    by at least `threshold` (dollars) from the very FIRST snapshot -- i.e.
    a move that has HELD across every recent check, not just happened
    once. If any recent check falls back under the threshold, this
    returns None -- that's exactly the "team was down, then came back"
    case, and it's deliberately rejected rather than acted on.
    """
    if len(history) < required_checks + 1:
        return None  # need a baseline snapshot plus N confirming ones

    baseline = history[0]
    recent = history[-required_checks:]

    for side in ("away", "home"):
        base_price = baseline.get(f"{side}_price")
        if base_price is None:
            continue
        moves = []
        ok = True
        for snap in recent:
            p = snap.get(f"{side}_price")
            if p is None:
                ok = False
                break
            moves.append(p - base_price)
        if not ok or not moves:
            continue
        if all(m >= threshold for m in moves):
            return side, base_price, recent[-1][f"{side}_price"]
    return None


def monitor_live_games(client, send_discord_fn, webhook):
    """
    Called on the fast (BTC-speed) cycle -- polls Kalshi's OWN current
    price for every tracked in-progress game, appends to that game's
    price history, and decides AT MOST ONCE per game whether a sustained
    move looks real. Once decided (or once the game is presumed over),
    that game is never re-evaluated again -- no reacting to the score
    swinging back the other way after a decision has been made.
    """
    if not LIVE_TRADING_ENABLED:
        return
    try:
        data = _load()
        now = datetime.now(timezone.utc)
        changed = False

        for key, game in list(data["games"].items()):
            try:
                start_dt = datetime.fromisoformat(game["start_time"].replace("Z", "+00:00"))
            except Exception:
                del data["games"][key]
                changed = True
                continue

            age_hours = (now - start_dt).total_seconds() / 3600
            if age_hours > MAX_GAME_AGE_HOURS:
                del data["games"][key]
                changed = True
                continue

            if game.get("decided"):
                continue

            try:
                away_market = client.get_market(game["away_ticker"])
                home_market = client.get_market(game["home_ticker"])
                away_price = getattr(away_market, "yes_ask_dollars", None)
                home_price = getattr(home_market, "yes_ask_dollars", None)
                away_price = float(away_price) if away_price else None
                home_price = float(home_price) if home_price else None
            except Exception:
                continue
            if away_price is None or home_price is None:
                continue

            game["price_history"].append({"at": now.isoformat(), "away_price": away_price, "home_price": home_price})
            game["price_history"] = game["price_history"][-MAX_HISTORY_PER_GAME:]
            changed = True

            result = _sustained_direction(game["price_history"], MOVE_THRESHOLD_CENTS / 100, REQUIRED_CONSISTENT_CHECKS)
            if result:
                side, base_price, current_price = result
                team = game["away_team"] if side == "away" else game["home_team"]
                ticker = game["away_ticker"] if side == "away" else game["home_ticker"]
                made = pt.record_live_moneyline_pick(
                    league=game["league"], event_id=game["event_id"],
                    away_team=game["away_team"], home_team=game["home_team"],
                    picked_team=team, kalshi_ticker=ticker, entry_price=current_price,
                    opening_price=base_price,
                )
                game["decided"] = True
                if made:
                    send_discord_fn(
                        webhook,
                        f"[LIVE, paper only] {team} moved from {base_price*100:.0f}c to {current_price*100:.0f}c "
                        f"and held for {REQUIRED_CONSISTENT_CHECKS} straight checks -- paper-backing {team} "
                        f"in-game ({game['league'].upper()})."
                    )

        if changed:
            _save(data)
    except Exception as e:
        print(f"[live_trading] monitor_live_games error: {e}")
