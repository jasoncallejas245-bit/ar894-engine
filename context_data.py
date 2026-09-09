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
    "wnba": "basketball/wnba",
    # UFC/ATP are individual-athlete sports with no team-injury-report
    # (or "tied score") concept on ESPN's team/scoreboard endpoints --
    # deliberately not included here.
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


def get_scoreboard(league):
    """
    Raw ESPN scoreboard events for a league right now (today's games,
    whatever state they're in -- pre/in/post). Best-effort like
    everything else here: [] on any failure, never raises. Callers
    fetch this ONCE per league per check and match multiple tracked
    games against the same result, rather than one request per game.
    """
    path = ESPN_LEAGUE_PATHS.get(league)
    if not path:
        return []
    try:
        data = _fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard")
        return data.get("events", [])
    except Exception as e:
        print(f"[context_data] {league} scoreboard fetch failed: {e}")
        return []


def get_live_score_from_scoreboard(events, league, away_team, home_team):
    """
    Matches a specific game (by team name, same fuzzy matching used for
    injuries/venue) against an already-fetched scoreboard, and returns
    {"state": "pre"|"in"|"post", "away_score": int, "home_score": int}
    if found and both scores are readable, else None. Matches by ESPN's
    own numeric team ID (not name strings) once each team is resolved,
    since that's exact where name matching could be ambiguous.
    """
    away_id = _find_team_id(league, away_team)
    home_id = _find_team_id(league, home_team)
    if not away_id or not home_id:
        return None
    try:
        for event in events:
            comps = (event.get("competitions") or [{}])[0].get("competitors", [])
            ids = {c.get("team", {}).get("id") for c in comps}
            if away_id not in ids or home_id not in ids:
                continue
            state = ((event.get("status") or {}).get("type") or {}).get("state")
            scores = {}
            for c in comps:
                tid = c.get("team", {}).get("id")
                try:
                    scores[tid] = int(c.get("score"))
                except (TypeError, ValueError):
                    continue
            if away_id not in scores or home_id not in scores:
                return None
            return {"state": state, "away_score": scores[away_id], "home_score": scores[home_id]}
    except Exception as e:
        print(f"[context_data] {league} scoreboard match failed: {e}")
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
    """
    Home team's venue coordinates, used as a proxy for the game venue
    (correct for the large majority of games -- wrong only for a genuine
    neutral-site game, which this doesn't try to detect).

    Confirmed live during development: ESPN's team endpoint does NOT
    return latitude/longitude directly (an earlier version of this
    function assumed a `venue.grid` field that doesn't exist and always
    returned None). What it DOES return is a street address --
    `team.franchise.venue.address` with city/state/zipCode -- so this
    geocodes that zip code via Zippopotam.us (free, no API key, confirmed
    working) to get real coordinates. Returns (None, None) if the team
    can't be matched or any step fails.
    """
    team_id = _find_team_id(league, home_team)
    path = ESPN_LEAGUE_PATHS.get(league)
    if not team_id or not path:
        return None, None
    cache_key = (league, team_id)
    if cache_key in _venue_cache:
        return _venue_cache[cache_key]

    result = (None, None)
    try:
        team_data = _fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}")
        address = team_data.get("team", {}).get("franchise", {}).get("venue", {}).get("address", {})
        zip_code = address.get("zipCode")
        country = (address.get("country") or "USA").upper()
        if zip_code and country in ("USA", "US"):
            geo = _fetch_json(f"https://api.zippopotam.us/us/{zip_code}")
            place = (geo.get("places") or [{}])[0]
            lat, lon = place.get("latitude"), place.get("longitude")
            if lat is not None and lon is not None:
                result = (float(lat), float(lon))
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


def get_team_recent_form(league, team_name, num_games=10):
    """
    "How has this team been playing lately" -- a real signal (2026-09-09):
    going through the user's own manual betting history (a year+ of
    Gemini conversations building parlays) showed recent team form and
    the starting-pitcher matchup were the two things actually driving
    picks, not just the raw sportsbook odds AR894's edge math already
    uses. This adds recent form; get_probable_pitcher_note below adds
    the pitcher piece.

    Returns a short string like "7-3 in last 10, won 4 in a row" (or
    "lost 2 in a row" / "" if the streak is 1) using ESPN's free team
    schedule endpoint -- the same free-API pattern as the rest of this
    module. None if the team can't be matched or the lookup fails.
    """
    team_id = _find_team_id(league, team_name)
    path = ESPN_LEAGUE_PATHS.get(league)
    if not team_id or not path:
        return None
    try:
        data = _fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}/schedule")
        events = data.get("events", [])
        completed = [
            e for e in events
            if e.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed")
        ]
        completed.sort(key=lambda e: e.get("date", ""))
        recent = completed[-num_games:]
        if not recent:
            return None

        results = []  # True = this team won
        for e in recent:
            comp = e["competitions"][0]
            for c in comp.get("competitors", []):
                if c.get("team", {}).get("id") == str(team_id):
                    results.append(bool(c.get("winner")))
                    break

        if not results:
            return None

        wins = sum(1 for r in results if r)
        losses = len(results) - wins

        streak_len = 1
        streak_won = results[-1]
        for r in reversed(results[:-1]):
            if r == streak_won:
                streak_len += 1
            else:
                break
        streak_note = ""
        if streak_len >= 2:
            streak_note = f", {'won' if streak_won else 'lost'} {streak_len} in a row"

        return f"{wins}-{losses} in last {len(results)}{streak_note}"
    except Exception as e:
        print(f"[context_data] recent form lookup failed for {team_name}: {e}")
        return None


def get_probable_pitcher_note(away_team, home_team):
    """
    MLB only: today's probable starting pitchers and their ERA for both
    teams, straight from ESPN's public scoreboard (a free "probables"
    field on each competitor -- confirmed live 2026-09-09, no API key
    needed). This was the #1 signal in the user's own manual picks
    ("Cam Schlittler 2.17 ERA vs Patrick Sandoval 4.58 ERA" style
    reasoning shows up over and over in their betting history) --
    informational for now, same as injuries/weather. None if today's
    game or a probable pitcher isn't listed for either side.
    """
    try:
        events = get_scoreboard("mlb")
        away_norm, home_norm = _normalize(away_team), _normalize(home_team)
        for e in events:
            comp = e.get("competitions", [{}])[0]
            teams_here = {_normalize(c.get("team", {}).get("displayName", "")) for c in comp.get("competitors", [])}
            teams_here |= {_normalize(c.get("team", {}).get("shortDisplayName", "")) for c in comp.get("competitors", [])}
            if not ((away_norm in teams_here or any(away_norm in t or t in away_norm for t in teams_here if t))
                    and (home_norm in teams_here or any(home_norm in t or t in home_norm for t in teams_here if t))):
                continue

            parts = []
            for c in comp.get("competitors", []):
                probables = c.get("probables") or []
                if not probables:
                    continue
                p = probables[0]
                name = p.get("athlete", {}).get("displayName")
                era = next((s.get("displayValue") for s in p.get("statistics", []) if s.get("name") == "ERA"), None)
                team_name = c.get("team", {}).get("shortDisplayName") or c.get("team", {}).get("displayName")
                if name:
                    parts.append(f"{team_name}: {name}" + (f" ({era} ERA)" if era else ""))
            if parts:
                return "Starting pitchers -- " + " | ".join(parts)
            return None
        return None
    except Exception as e:
        print(f"[context_data] probable pitcher lookup failed for {away_team}/{home_team}: {e}")
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

    if league == "mlb":
        pitcher_note = get_probable_pitcher_note(away_team, home_team)
        if pitcher_note:
            lines.append(pitcher_note)

    for team in (away_team, home_team):
        form = get_team_recent_form(league, team)
        if form:
            lines.append(f"{team} recent form: {form}")

    for team in (away_team, home_team):
        injuries = get_team_injuries(league, team)
        if injuries:
            lines.append(f"{team} injuries: " + "; ".join(injuries))

    lat, lon = get_venue_latlon(league, home_team)
    forecast = get_venue_forecast(lat, lon)
    if forecast:
        lines.append(f"Weather at {home_team}: {forecast}")

    return "\n".join(lines) if lines else None
