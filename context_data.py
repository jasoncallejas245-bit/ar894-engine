"""
Free contextual data for "too close to call" moneyline games: injury
reports from ESPN's free, unauthenticated public API, and outdoor-venue
weather from the National Weather Service API. Both are free and need no
API key.

Scope and honesty about what this is: matching a sportsbook's team name
to ESPN's internal team ID, and then judging whether an injury report is
actually significant enough to swing a game, are both much harder
problems than just fetching data. This module does the fetching and
best-effort name matching, and produces a short human-readable note --
it does NOT try to quantify "how many percentage points should this move
the fair probability." That's why it's wired in as an INFORMATIONAL
layer (attached to paper picks + Discord alerts for the close-call
bucket only) rather than something that changes which side gets picked.
If the notes prove reliable over time, the next step would be turning
this into an actual probability adjustment -- that's a deliberate choice
to keep now, not an oversight.

Every public function here is best-effort and NEVER raises -- a bad
team-name match or a flaky upstream API just means an empty/None result,
never a crash that takes down the trading loop over this.
"""
import requests
from datetime import date, datetime, timezone

ESPN_LEAGUE_PATHS = {
    "nfl": "football/nfl",
    "ncaaf": "football/college-football",
    "mlb": "baseball/mlb",
    # UFC/ATP are individual-athlete sports with no team-injury-report
    # concept on ESPN's team endpoints -- deliberately not included here.
}

# ESPN's "core" API (a separate product from the "site" API above) is what
# actually holds injury data -- but it's hypermedia: the team-injuries
# endpoint returns a season-long list of $ref LINKS, not injury details,
# and that list mixes in every injury-report EVENT for the season,
# including ones later resolved back to "Active" (confirmed live: the
# first entry checked during development had status "Active" -- a
# depth-chart note, not a current injury). So this has to resolve
# individual links and filter, not just read the list.
CORE_SPORT_LEAGUE = {
    "nfl": ("football", "nfl"),
    "ncaaf": ("football", "college-football"),
    "mlb": ("baseball", "mlb"),
}

# Confirmed live: a resolved/non-injury entry reports status "Active".
# Anything else (Out, Questionable, Doubtful, Injured Reserve, etc.)
# is treated as a real, current injury concern.
_NON_NOTABLE_STATUSES = {"active"}

# Injury-report events older than this are treated as stale noise, not a
# "current" injury -- a two-week-old "Questionable" tag isn't useful
# context for tonight's game.
_INJURY_MAX_AGE_DAYS = 14

_UA = {"User-Agent": "ar894-engine (personal trading bot; contact via GitHub repo)"}

_team_id_cache = {}    # league -> {normalized_name: team_id}
_venue_cache = {}      # (league, team_id) -> (lat, lon) or (None, None)
_injury_cache = {}     # (league, team_id) -> {"date": "YYYY-MM-DD", "injuries": [...]}


def _fetch_json(url, params=None):
    resp = requests.get(url, params=params, headers=_UA, timeout=8)
    resp.raise_for_status()
    return resp.json()


def _normalize(name):
    return (name or "").upper().replace(".", "").replace("  ", " ").strip()


def _load_team_ids(league):
    if league in _team_id_cache:
        return _team_id_cache[league]
    path = ESPN_LEAGUE_PATHS.get(league)
    if not path:
        return {}
    mapping = {}
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams",
            params={"limit": 200}, headers=_UA, timeout=8,
        )
        resp.raise_for_status()
        teams = resp.json()["sports"][0]["leagues"][0]["teams"]
        for entry in teams:
            team = entry.get("team", {})
            team_id = team.get("id")
            if not team_id:
                continue
            for key in (team.get("displayName"), team.get("shortDisplayName"), team.get("name"), team.get("location")):
                if key:
                    mapping[_normalize(key)] = team_id
    except Exception as e:
        print(f"[context_data] {league} team list fetch failed: {e}")
    _team_id_cache[league] = mapping
    return mapping


def _find_team_id(league, team_name):
    mapping = _load_team_ids(league)
    norm = _normalize(team_name)
    if norm in mapping:
        return mapping[norm]
    for key, team_id in mapping.items():
        if key and (key in norm or norm in key):
            return team_id
    return None


def get_team_injuries(league, team_name, max_items=5, max_checked=15):
    """
    Current, notable injuries for a team -- e.g. an actual "Out" or
    "Questionable" tag from roughly the last two weeks, not the season's
    entire injury-report history. Resolves ESPN's core-API hypermedia
    list one link at a time (status + date), and a second link for the
    player's name only for entries that pass the status/recency filter --
    so a fully healthy team costs one list fetch and nothing else, not a
    guaranteed pile of requests. Cached per (league, team) per calendar
    day, since injury reports don't meaningfully change every 5 minutes
    and this would otherwise re-resolve the same links every scan cycle.
    Returns [] if none found, the team couldn't be matched, or any part
    of the lookup failed -- never raises.
    """
    team_id = _find_team_id(league, team_name)
    sport_league = CORE_SPORT_LEAGUE.get(league)
    if not team_id or not sport_league:
        return []

    today_str = date.today().isoformat()
    cache_key = (league, team_id)
    cached = _injury_cache.get(cache_key)
    if cached and cached["date"] == today_str:
        return cached["injuries"][:max_items]

    sport, core_league = sport_league
    try:
        listing = _fetch_json(
            f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{core_league}/teams/{team_id}/injuries"
        )
    except Exception as e:
        print(f"[context_data] injuries list fetch failed for {team_name}: {e}")
        return []

    refs = [item.get("$ref") for item in (listing.get("items") or [])[:max_checked] if item.get("$ref")]
    cutoff = datetime.now(timezone.utc).timestamp() - _INJURY_MAX_AGE_DAYS * 86400

    notable = []
    for ref in refs:
        try:
            detail = _fetch_json(ref)
        except Exception:
            continue

        status = (detail.get("status") or "").strip()
        if not status or status.lower() in _NON_NOTABLE_STATUSES:
            continue

        entry_date = detail.get("date")
        try:
            entry_ts = datetime.fromisoformat(entry_date.replace("Z", "+00:00")).timestamp() if entry_date else None
        except Exception:
            entry_ts = None
        if entry_ts is not None and entry_ts < cutoff:
            continue  # stale -- an old report, not a current concern

        athlete_name = "?"
        athlete_ref = (detail.get("athlete") or {}).get("$ref")
        if athlete_ref:
            try:
                athlete_name = _fetch_json(athlete_ref).get("displayName", "?")
            except Exception:
                pass

        notable.append(f"{athlete_name} - {status}")
        if len(notable) >= max_items:
            break

    _injury_cache[cache_key] = {"date": today_str, "injuries": notable}
    return notable


def get_venue_latlon(league, home_team):
    """Home team's venue coordinates via ESPN's team endpoint, used as a
    proxy for the game venue (correct for the large majority of games --
    wrong only for a genuine neutral-site game, which this doesn't try to
    detect). Returns (None, None) if unavailable."""
    team_id = _find_team_id(league, home_team)
    path = ESPN_LEAGUE_PATHS.get(league)
    if not team_id or not path:
        return None, None
    cache_key = (league, team_id)
    if cache_key in _venue_cache:
        return _venue_cache[cache_key]
    result = (None, None)
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}",
            headers=_UA, timeout=8,
        )
        resp.raise_for_status()
        venue = resp.json().get("team", {}).get("venue", {})
        grid = venue.get("grid") or {}
        lat, lon = grid.get("latitude"), grid.get("longitude")
        if lat is not None and lon is not None:
            result = (lat, lon)
    except Exception as e:
        print(f"[context_data] venue lookup failed for {home_team}: {e}")
    _venue_cache[cache_key] = result
    return result


def get_venue_forecast(lat, lon):
    """National Weather Service hourly forecast for a lat/lon, as a short
    string, or None if unavailable. Free, no API key -- just requires a
    descriptive User-Agent, which NWS's docs ask for."""
    if lat is None or lon is None:
        return None
    try:
        points_resp = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=_UA, timeout=8)
        points_resp.raise_for_status()
        forecast_url = points_resp.json()["properties"]["forecastHourly"]
        fc_resp = requests.get(forecast_url, headers=_UA, timeout=8)
        fc_resp.raise_for_status()
        period = fc_resp.json()["properties"]["periods"][0]
        return f"{period.get('shortForecast')}, {period.get('temperature')}{period.get('temperatureUnit')}, wind {period.get('windSpeed')}"
    except Exception as e:
        print(f"[context_data] weather fetch failed: {e}")
        return None


def get_context_note(league, away_team, home_team):
    """
    Best-effort, human-readable note for a too-close-to-call game:
    notable injuries for both teams, plus weather at the home venue.
    Returns None if nothing notable was found (including if every
    lookup failed) -- callers should treat None as "no context
    available," not "confirmed nothing going on."
    """
    if league not in ESPN_LEAGUE_PATHS:
        return None  # UFC/ATP: no team-injury concept here

    lines = []
    for team in (away_team, home_team):
        injuries = get_team_injuries(league, team)
        if injuries:
            lines.append(f"{team} injuries: " + "; ".join(injuries))

    lat, lon = get_venue_latlon(league, home_team)
    forecast = get_venue_forecast(lat, lon)
    if forecast:
        lines.append(f"Weather at {home_team}: {forecast}")

    return "\n".join(lines) if lines else None
