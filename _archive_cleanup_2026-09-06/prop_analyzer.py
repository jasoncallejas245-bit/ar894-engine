import os
import requests
from datetime import datetime

SHARPAPI_KEY = os.environ["SHARPAPI_KEY"]
BASE_URL = "https://api.sharpapi.io/api/v1/odds"
EDGE_THRESHOLD = 0.545  # 54.5% implied probability minimum to flag

LEAGUES = ["nfl", "ncaaf"]


def format_iso_time(iso_string):
    """Converts ISO 8601 UTC string to readable local Day, Date, and Kickoff Time."""
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        local_dt = dt.astimezone()
        return local_dt.strftime("%a, %b %d @ %I:%M %p %Z")
    except Exception:
        return iso_string


def scan_full_slate():
    print("\n==================================================")
    print("   AUTONOMOUS MULTI-SLATE +EV SCANNER (SharpAPI)     ")
    print("==================================================")

    all_positive_ev_legs = []
    total_rows_scanned = 0

    headers = {"X-API-Key": SHARPAPI_KEY}

    for league in LEAGUES:
        print(f"\n🔍 Querying active {league.upper()} matchups...")

        resp = requests.get(
            BASE_URL,
            params={"league": league, "market": "main", "limit": 200},
            headers=headers,
        )

        if resp.status_code != 200:
            print(f"❌ Failed to fetch odds for {league.upper()} (Status Code {resp.status_code})")
            print(f"   {resp.text[:300]}")
            continue

        payload = resp.json()
        rows = payload.get("data", [])

        if not rows:
            print(f"⚠️ No active {league.upper()} odds returned right now.")
            continue

        for row in rows:
            total_rows_scanned += 1
            implied_prob = row.get("odds_probability")
            if implied_prob is None:
                continue

            if implied_prob >= EDGE_THRESHOLD:
                edge = (implied_prob - EDGE_THRESHOLD) * 100
                all_positive_ev_legs.append({
                    "league": league.upper(),
                    "matchup": f"{row.get('away_team')} @ {row.get('home_team')}",
                    "game_time": format_iso_time(row.get("event_start_time", "")),
                    "market": row.get("market_type"),
                    "selection": row.get("selection"),
                    "line": row.get("line"),
                    "odds": row.get("odds_american"),
                    "prob": implied_prob,
                    "edge": edge,
                    "book": row.get("sportsbook"),
                })

    print("\n==================================================")
    print(f"📊 SUMMARY: Evaluated {total_rows_scanned} odds rows across NFL & CFB.")

    if not all_positive_ev_legs:
        print("💡 NO POSITIVE EV LINES DETECTED RIGHT NOW.")
    else:
        all_positive_ev_legs.sort(key=lambda x: x["prob"], reverse=True)
        print("\n🎯 TOP EDGES FOUND:")
        for idx, leg in enumerate(all_positive_ev_legs[:5], 1):
            point_str = f" {leg['line']}" if leg["line"] is not None else ""
            print(f"  {idx}. [{leg['league']}] {leg['market'].upper()}: {leg['selection']}{point_str}")
            print(f"     ► Matchup: {leg['matchup']}")
            print(f"     ► Kickoff: 🗓️ {leg['game_time']}")
            print(f"     ► {leg['odds']} ({leg['book']}) -> Implied: {leg['prob']*100:.1f}% (+{leg['edge']:.1f}% Edge)\n")

    print("==================================================\n")
    return all_positive_ev_legs


if __name__ == "__main__":
    scan_full_slate()
