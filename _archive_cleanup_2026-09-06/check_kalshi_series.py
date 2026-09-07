import os
from kalshi_python import Configuration, KalshiClient

KEY_ID = os.environ["KALSHI_KEY_ID"]
PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]

with open(PRIVATE_KEY_PATH, "r") as f:
    private_key_content = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = KEY_ID
config.private_key_pem = private_key_content

with KalshiClient(config) as client:
    print("=== get_series() with no arguments ===")
    try:
        series_response = client.get_series()
        all_series = getattr(series_response, "series", None) or []
        print(f"Total series returned: {len(all_series)}")
        for s in all_series:
            ticker = getattr(s, "ticker", "")
            title = getattr(s, "title", "") or ""
            if "NFL" in ticker.upper() or "NFL" in title.upper() or "CFB" in ticker.upper() or "NCAAF" in title.upper():
                print(f"  {ticker}: {title}")
    except Exception as e:
        print(f"get_series() failed: {e}")

    print("\n=== Open markets with ticker starting with common NFL/CFB series prefixes ===")
    # Try filtering directly by likely series_ticker prefixes
    candidate_prefixes = ["KXNFLGAME", "KXNFL", "KXNCAAFGAME", "KXNCAAF", "KXCFB"]
    for prefix in candidate_prefixes:
        try:
            resp = client.get_markets(series_ticker=prefix, status="open", limit=10)
            markets = getattr(resp, "markets", None) or []
            print(f"\n  Prefix '{prefix}': {len(markets)} markets found")
            for m in markets[:5]:
                print(f"    {m.ticker}: {m.title}")
        except Exception as e:
            print(f"  Prefix '{prefix}' failed: {e}")
