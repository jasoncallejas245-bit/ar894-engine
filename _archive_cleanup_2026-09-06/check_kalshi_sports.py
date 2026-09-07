import os
from kalshi_python import Configuration, KalshiClient

KEY_ID = os.environ["KALSHI_KEY_ID"]
PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]

with open(PRIVATE_KEY_PATH, "r") as f:
    private_key_content = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = KEY_ID
config.private_key_pem = private_key_content

SPORTS_KEYWORDS = ["NFL", "NCAAF", "FOOTBALL", "NBA", "NHL", "MLB", "SUPER BOWL", "GAME"]

with KalshiClient(config) as client:
    print("=== Trying get_series() with no filters ===")
    try:
        series_response = client.get_series(limit=1000)
        all_series = getattr(series_response, "series", None) or []
        print(f"Total series returned: {len(all_series)}")
        print("\n--- Series matching sports keywords ---")
        for s in all_series:
            title = getattr(s, "title", "") or ""
            ticker = getattr(s, "ticker", "")
            if any(kw in title.upper() for kw in SPORTS_KEYWORDS):
                print(f"  {ticker}: {title}")
    except Exception as e:
        print(f"get_series() failed: {e}")

    print("\n=== Open markets with sports-related titles (scanning up to 1000) ===")
    try:
        markets_response = client.get_markets(status="open", limit=1000)
        all_markets = getattr(markets_response, "markets", None) or []
        print(f"Total open markets returned: {len(all_markets)}")
        matches = 0
        for m in all_markets:
            title = getattr(m, "title", "") or ""
            if any(kw in title.upper() for kw in SPORTS_KEYWORDS):
                ticker = getattr(m, "ticker", "")
                print(f"  {ticker}: {title}")
                matches += 1
        print(f"\nSports-related open markets found: {matches}")
    except Exception as e:
        print(f"get_markets() failed: {e}")
