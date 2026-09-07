import os
import requests

SHARPAPI_KEY = os.environ["SHARPAPI_KEY"]

for league, teams in [("ncaaf", ["UTSA", "Texas"]), ("ncaaf", ["Rutgers", "Boston College"])]:
    resp = requests.get(
        "https://api.sharpapi.io/api/v1/odds",
        params={"league": league, "market": "main", "limit": 200},
        headers={"X-API-Key": SHARPAPI_KEY},
    )
    rows = resp.json().get("data", [])

    matching = [
        r for r in rows
        if r.get("market_type") == "moneyline" and r.get("is_main_line") is True
        and any(t.upper() in (r.get("home_team", "") + r.get("away_team", "")).upper() for t in teams)
    ]

    print(f"\n=== {' vs '.join(teams)} ===")
    for r in matching:
        print(f"  {r.get('sportsbook')}: {r.get('selection')} -> odds_probability={r.get('odds_probability')}, "
              f"away={r.get('away_team')}, home={r.get('home_team')}, event_id={r.get('event_id')}")
