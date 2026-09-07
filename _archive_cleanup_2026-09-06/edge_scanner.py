import os
import requests
from datetime import datetime
from collections import defaultdict

SHARPAPI_KEY = os.environ["SHARPAPI_KEY"]
BASE_URL = "https://api.sharpapi.io/api/v1/odds"

MIN_EDGE_PCT = 2.0
LEAGUES = ["nfl", "ncaaf"]


def format_iso_time(iso_string):
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        return dt.astimezone().strftime("%a, %b %d @ %I:%M %p %Z")
    except Exception:
        return iso_string


def remove_vig_two_way(prob_a, prob_b):
    total = prob_a + prob_b
    if total == 0:
        return None, None
    return prob_a / total, prob_b / total


def fetch_odds(league):
    resp = requests.get(
        BASE_URL,
        params={"league": league, "market": "main", "limit": 200},
        headers={"X-API-Key": SHARPAPI_KEY},
    )
    if resp.status_code != 200:
        print(f"❌ Failed to fetch odds for {league.upper()} (Status {resp.status_code}): {resp.text[:200]}")
        return []
    return resp.json().get("data", [])


def find_real_edges():
    print("\n==================================================")
    print("   NO-VIG EDGE SCANNER (DraftKings vs FanDuel)      ")
    print("==================================================")

    all_edges = []

    for league in LEAGUES:
        print(f"\n🔍 Scanning {league.upper()}...")
        rows = fetch_odds(league)
        if not rows:
            continue

        # CRITICAL: the grouping key must include `line` — otherwise
        # different alternate lines (e.g. total 9.5 vs total 49.5) get
        # merged together as if they were the same bet. is_main_line
        # also filters out cohort-pending / ambiguous rows.
        grouped = defaultdict(dict)  # (event_id, market_type, line, selection) -> {book: row}
        for row in rows:
            if row.get("is_main_line") is not True:
                continue  # skip alt lines and cohort-pending rows entirely for now
            key = (row.get("event_id"), row.get("market_type"), row.get("line"), row.get("selection"))
            grouped[key][row.get("sportsbook")] = row

        # Now find the opposite side for each (event_id, market_type, line)
        by_event_market_line = defaultdict(set)
        for (event_id, market_type, line, selection) in grouped.keys():
            by_event_market_line[(event_id, market_type, line)].add(selection)

        seen_pairs = set()

        for (event_id, market_type, line), selections in by_event_market_line.items():
            selections = list(selections)
            if len(selections) != 2:
                continue
            sel_a, sel_b = selections
            pair_key = (event_id, market_type, line)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            rows_a = grouped[(event_id, market_type, line, sel_a)]
            rows_b = grouped[(event_id, market_type, line, sel_b)]

            common_books = set(rows_a.keys()) & set(rows_b.keys())
            if len(common_books) < 2:
                continue

            novig_a_by_book = {}
            novig_b_by_book = {}
            for book in common_books:
                pa = rows_a[book].get("odds_probability")
                pb = rows_b[book].get("odds_probability")
                if pa is None or pb is None:
                    continue
                na, nb = remove_vig_two_way(pa, pb)
                if na is None:
                    continue
                novig_a_by_book[book] = na
                novig_b_by_book[book] = nb

            if not novig_a_by_book:
                continue

            fair_prob_a = sum(novig_a_by_book.values()) / len(novig_a_by_book)
            fair_prob_b = sum(novig_b_by_book.values()) / len(novig_b_by_book)

            for selection, fair_prob, rows_dict in [
                (sel_a, fair_prob_a, rows_a),
                (sel_b, fair_prob_b, rows_b),
            ]:
                for book, row in rows_dict.items():
                    book_implied = row.get("odds_probability")
                    if book_implied is None or book_implied >= fair_prob:
                        continue

                    edge_pct = (fair_prob - book_implied) * 100
                    if edge_pct < MIN_EDGE_PCT:
                        continue

                    all_edges.append({
                        "league": league.upper(),
                        "matchup": f"{row.get('away_team')} @ {row.get('home_team')}",
                        "game_time": format_iso_time(row.get("event_start_time", "")),
                        "market": market_type,
                        "selection": selection,
                        "line": line,
                        "book": book,
                        "book_odds": row.get("odds_american"),
                        "book_implied_prob": book_implied,
                        "fair_prob": fair_prob,
                        "edge_pct": edge_pct,
                    })

    print("\n==================================================")
    if not all_edges:
        print("💡 NO GENUINE +EV EDGES DETECTED RIGHT NOW.")
        print("   (Normal — two-book markets rarely diverge enough to matter.)")
    else:
        all_edges.sort(key=lambda x: x["edge_pct"], reverse=True)
        print(f"🎯 {len(all_edges)} GENUINE EDGE(S) FOUND:\n")
        for idx, e in enumerate(all_edges[:10], 1):
            line_str = f" {e['line']}" if e["line"] is not None else ""
            print(f"  {idx}. [{e['league']}] {e['market'].upper()}: {e['selection']}{line_str} on {e['book']}")
            print(f"     ► Matchup: {e['matchup']}")
            print(f"     ► Kickoff: 🗓️ {e['game_time']}")
            print(f"     ► Book price: {e['book_odds']} (implied {e['book_implied_prob']*100:.1f}%) vs fair {e['fair_prob']*100:.1f}%")
            print(f"     ► Real edge: +{e['edge_pct']:.2f}%\n")

    print("==================================================\n")
    return all_edges


if __name__ == "__main__":
    find_real_edges()
