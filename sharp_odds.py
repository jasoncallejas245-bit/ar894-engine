"""
Pinnacle odds via The Odds API (the-odds-api.com), added 2026-10-05.

Why: SharpAPI's free tier only gives DraftKings + FanDuel (60s delayed).
The 10/5 review showed that "consensus" is worse than Kalshi's own price.
Pinnacle is the standard sharp benchmark, and The Odds API includes it
(region "eu", bookmaker key "pinnacle") from its free plan up.

Off unless ODDS_API_KEY is set. Budget-aware: each (league) fetch costs 1
credit (one market, one bookmaker), cached on disk and refreshed at most
every ODDS_API_MIN_INTERVAL_MIN minutes. Defaults fit the free plan
(500 credits/month): NFL + NCAAF every 6 hours = ~240/month. On the $30
plan (20K/month) set ODDS_API_LEAGUES to more leagues and the interval to
~15 minutes.

Never raises into the caller -- returns None when there's no data.
"""
import os
import re
from datetime import datetime, timezone

import requests

from state_io import atomic_write_json, safe_read_json

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
CACHE_FILE = os.path.join(DATA_DIR, "sharp_odds_cache.json")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_LEAGUES = {x.strip().lower() for x in os.getenv("ODDS_API_LEAGUES", "nfl,ncaaf").split(",") if x.strip()}
ODDS_API_MIN_INTERVAL_MIN = float(os.getenv("ODDS_API_MIN_INTERVAL_MIN", "360"))
# Stop fetching when the account's remaining credits drop to this, so a
# misconfigured interval can't burn the whole month.
ODDS_API_RESERVE_CREDITS = int(os.getenv("ODDS_API_RESERVE_CREDITS", "20"))
SHARP_BOOK = os.getenv("SHARP_BOOK", "pinnacle")

SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "ufc": "mma_mixed_martial_arts",
}
BASE = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


def enabled():
    return bool(ODDS_API_KEY)


def _norm(name):
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace(" st.", " state").replace(" st ", " state ")).strip()


def names_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def _load_cache():
    return safe_read_json(CACHE_FILE, {"leagues": {}, "remaining": None, "last_error": None})


def _fetch(league, cache):
    sport = SPORT_KEYS.get(league)
    if not sport:
        return
    rem = cache.get("remaining")
    if rem is not None and rem <= ODDS_API_RESERVE_CREDITS:
        cache["last_error"] = f"stopped: only {rem} credits left"
        return
    try:
        resp = requests.get(BASE.format(sport=sport), params={
            "apiKey": ODDS_API_KEY, "markets": "h2h", "bookmakers": SHARP_BOOK, "oddsFormat": "decimal",
        }, timeout=20)
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            try:
                cache["remaining"] = int(float(remaining))
            except ValueError:
                pass
        if resp.status_code != 200:
            cache["last_error"] = f"{league}: HTTP {resp.status_code} {resp.text[:200]}"
            cache["leagues"].setdefault(league, {})["fetched_at"] = datetime.now(timezone.utc).isoformat()
            return
        events = []
        for ev in resp.json():
            for bk in ev.get("bookmakers", []):
                if bk.get("key") != SHARP_BOOK:
                    continue
                for mk in bk.get("markets", []):
                    if mk.get("key") != "h2h":
                        continue
                    outs = [o for o in mk.get("outcomes", []) if o.get("price")]
                    if len(outs) != 2:
                        continue  # skip 3-way (draw) markets
                    implied = [1.0 / float(o["price"]) for o in outs]
                    total = sum(implied)
                    events.append({
                        "home": ev.get("home_team"), "away": ev.get("away_team"),
                        "commence": ev.get("commence_time"),
                        "fair": {outs[i]["name"]: implied[i] / total for i in range(2)},
                        "updated": bk.get("last_update"),
                    })
        cache["leagues"][league] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "events": events}
        cache["last_error"] = None
    except Exception as e:
        cache["last_error"] = f"{league}: {e}"


def refresh_if_stale(league):
    """Fetch this league's sharp odds if the cache is older than the interval."""
    league = (league or "").lower()
    if not enabled() or league not in ODDS_API_LEAGUES:
        return
    cache = _load_cache()
    entry = cache["leagues"].get(league) or {}
    fetched = entry.get("fetched_at")
    if fetched:
        try:
            age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds() / 60
            if age_min < ODDS_API_MIN_INTERVAL_MIN:
                return
        except Exception:
            pass
    _fetch(league, cache)
    atomic_write_json(CACHE_FILE, cache)


def sharp_fair_prob(league, away_team, home_team, selection):
    """Pinnacle no-vig win probability for `selection`, or None."""
    league = (league or "").lower()
    if not enabled() or league not in ODDS_API_LEAGUES:
        return None
    try:
        events = (_load_cache()["leagues"].get(league) or {}).get("events") or []
        for ev in events:
            if names_match(ev["home"], home_team) and names_match(ev["away"], away_team):
                for name, p in ev["fair"].items():
                    if names_match(name, selection):
                        return p
    except Exception:
        return None
    return None


def sharp_age_minutes(league):
    """How old this league's cached Pinnacle odds are, in minutes (None if never fetched)."""
    try:
        fetched = (_load_cache()["leagues"].get((league or "").lower()) or {}).get("fetched_at")
        if not fetched:
            return None
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds() / 60, 1)
    except Exception:
        return None


def status():
    """Small summary for the dashboard."""
    if not enabled():
        return {"enabled": False}
    c = _load_cache()
    return {
        "enabled": True, "book": SHARP_BOOK, "remaining_credits": c.get("remaining"),
        "last_error": c.get("last_error"),
        "leagues": {k: {"fetched_at": v.get("fetched_at"), "events": len(v.get("events") or [])} for k, v in c.get("leagues", {}).items()},
    }
