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
    for prefix in ["KXNFLSPREAD", "KXNFLTOTAL", "KXNFLGAME"]:
        print(f"\n=== Prefix '{prefix}' — sample markets with full detail ===")
        try:
            resp = client.get_markets(series_ticker=prefix, status="open", limit=6)
            markets = getattr(resp, "markets", None) or []
            for m in markets:
                print(f"  ticker: {m.ticker}")
                print(f"    title: {m.title}")
                print(f"    yes_bid: {getattr(m, 'yes_bid', None)}  yes_ask: {getattr(m, 'yes_ask', None)}")
                print(f"    no_bid: {getattr(m, 'no_bid', None)}  no_ask: {getattr(m, 'no_ask', None)}")
                print(f"    close_time: {getattr(m, 'close_time', None)}")
                print(f"    event_ticker: {getattr(m, 'event_ticker', None)}")
                print()
        except Exception as e:
            print(f"  Failed: {e}")
