"""
Strategy version log (added 2026-10-05, at the user's request): every
change to how the engine picks bets gets a version number, starting at 1,
so results can always be compared version against version.

HOW TO ADD A VERSION: append a new dict to VERSIONS with the next number,
today's date, a short name and plain-English bullets of what changed, and
bump CURRENT_VERSION. Every new pick is stamped with CURRENT_VERSION
(paper_trading.make_moneyline_paper_picks), and engine_notes records the
switch the next time the worker starts.
"""

CURRENT_VERSION = 2

VERSIONS = [
    {
        "v": 1,
        "name": "DraftKings/FanDuel average",
        "start": "2026-09-13",
        "end": "2026-10-05",
        "changes": [
            "Win chance = average of DraftKings and FanDuel odds with the vig removed (SharpAPI free plan).",
            "Bet the favorite when that win chance beats Kalshi's price by 2%+ after fees, and is 60%+.",
            "$15 practice bets, held to the final score.",
        ],
        "verdict": "70 finished: 37 won, 33 lost, -$152.55 (-14.5%). Predicted 63% wins, got 53%; "
                   "Kalshi's own price (58%) was more accurate. Biggest 'edges' (8%+) won only 31%.",
    },
    {
        "v": 2,
        "name": "Edge cap + Kalshi blend + Pinnacle and limit-order tests",
        "start": "2026-10-05",
        "end": None,
        "changes": [
            "Edges over 7% are skipped as probable bad data.",
            "The bot's win chance is averaged halfway with Kalshi's price before deciding.",
            "Pinnacle odds (The Odds API) saved on every pick; picks Pinnacle alone likes get their own $100.",
            "Limit-order test: what if we waited 1 cent above the bid instead of paying the ask.",
            "Last price before the game is saved, to check if the market moves our way.",
            "Fresh $100 practice balance; v1 history kept for comparison.",
        ],
        "verdict": None,
    },
]


def version_of(pick):
    """Which strategy version made this pick (older picks are inferred)."""
    v = pick.get("strategy_version")
    if v:
        return int(v)
    return 2 if pick.get("raw_edge_pct") is not None else 1


def get(v):
    for item in VERSIONS:
        if item["v"] == v:
            return item
    return None
