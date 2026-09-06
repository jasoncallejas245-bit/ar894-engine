import requests
from datetime import datetime

API_KEY = "99897350c29c1d1a99eae69657625611"
MARKETS = "player_pass_yds"
PRIZEPICKS_POWER_THRESHOLD = 0.577  # 57.7% Implied Probability (-136 Sharp Odds)

SPORTS = [
    {"key": "americanfootball_nfl", "name": "NFL"},
    {"key": "americanfootball_ncaaf", "name": "College Football (CFB)"}
]

def american_to_implied(odds):
    """Converts American odds (-140) to implied probability percentage."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def format_commence_time(utc_string):
    """Converts UTC ISO string to readable local Day, Date, and Kickoff Time."""
    try:
        dt = datetime.fromisoformat(utc_string.replace('Z', '+00:00'))
        local_dt = dt.astimezone()
        return local_dt.strftime("%a, %b %d @ %I:%M %p %Z")
    except Exception:
        return utc_string

def scan_full_slate():
    print("\n==================================================")
    print("   AUTONOMOUS MULTI-SLATE +EV PROPS EVALUATOR     ")
    print("==================================================")
    
    all_positive_ev_legs = []
    total_games_scanned = 0

    for sport_info in SPORTS:
        sport_key = sport_info["key"]
        league_name = sport_info["name"]
        
        print(f"\n🔍 Querying active {league_name} matchups...")
        
        events_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
        events_response = requests.get(events_url, params={"apiKey": API_KEY})

        if events_response.status_code != 200:
            print(f"❌ Failed to fetch events for {league_name} (Status Code {events_response.status_code})")
            continue

        events = events_response.json()
        if not events:
            print(f"⚠️ No active {league_name} games listed on the board.")
            continue

        for event in events:
            total_games_scanned += 1
            event_id = event["id"]
            home_team = event["home_team"]
            away_team = event["away_team"]
            game_time = format_commence_time(event["commence_time"])

            odds_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
            odds_response = requests.get(
                odds_url,
                params={"apiKey": API_KEY, "regions": "us", "markets": MARKETS, "oddsFormat": "american"}
            )

            if odds_response.status_code != 200:
                continue

            odds_data = odds_response.json()
            bookmakers = odds_data.get("bookmakers", [])

            for book in bookmakers:
                book_name = book['title']
                for market in book.get("markets", []):
                    if market["key"] == "player_pass_yds":
                        for outcome in market.get("outcomes", []):
                            player = outcome.get("description")
                            direction = outcome.get("name")
                            line = outcome.get("point")
                            odds = outcome.get("price")
                            
                            implied_prob = american_to_implied(odds)
                            
                            # Filter for edge over PrizePicks break-even baseline
                            if implied_prob >= PRIZEPICKS_POWER_THRESHOLD:
                                edge = (implied_prob - PRIZEPICKS_POWER_THRESHOLD) * 100
                                all_positive_ev_legs.append({
                                    "league": league_name,
                                    "matchup": f"{away_team} @ {home_team}",
                                    "game_time": game_time,
                                    "player": player,
                                    "direction": direction,
                                    "line": line,
                                    "odds": odds,
                                    "prob": implied_prob,
                                    "edge": edge,
                                    "book": book_name
                                })

    print("\n==================================================")
    print(f"📊 SUMMARY: Evaluated {total_games_scanned} games across NFL & CFB.")
    
    if not all_positive_ev_legs:
        print("💡 NO POSITIVE EV LEGS DETECTED RIGHT NOW.")
        print("   Current sharp markets are balanced near 50/50 (-110).")
        print("   Re-run closer to game time when injury reports and heavy market volume move the lines.")
    else:
        # Sort legs by implied probability descending
        all_positive_ev_legs.sort(key=lambda x: x["prob"], reverse=True)
        
        print("\n🎯 RECOMMENDED $10 ENTRY (Highest Edge Legs):")
        top_legs = all_positive_ev_legs[:2]
        
        for idx, leg in enumerate(top_legs, 1):
            print(f"  {idx}. [{leg['league']}] {leg['player']} — {leg['direction']} {leg['line']} Passing Yards")
            print(f"     ► Matchup: {leg['matchup']}")
            print(f"     ► Kickoff: 🗓️ {leg['game_time']}")
            print(f"     ► Sharp Line: {leg['odds']} ({leg['book']}) -> Implied: {leg['prob']*100:.1f}% (+{leg['edge']:.1f}% Edge)\n")

        if len(top_legs) == 2:
            combined_hit_rate = top_legs[0]["prob"] * top_legs[1]["prob"]
            payout = 30.00  # 3.0x payout for 2-leg Power Play
            ev = (combined_hit_rate * payout) - 10.00
            
            print("📈 Quantitative Slip Metrics:")
            print(f"   ► Combined Hit Rate: {combined_hit_rate*100:.2f}%")
            print(f"   ► Projected Return: ${payout:.2f} (3.0x)")
            print(f"   ► Expected Value (EV): ${ev:+.2f}")
            print("💡 VERDICT: AUTO-APPROVED SLIP")

    print("==================================================\n")

if __name__ == "__main__":
    scan_full_slate()
