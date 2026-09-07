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
    # Check the higher-profile Notre Dame/Wisconsin game specifically
    for ticker in ["KXNCAAFGAME-26SEP06WISND-ND", "KXNCAAFGAME-26SEP06WISND-WIS"]:
        print(f"=== {ticker} ===")
        resp = client.get_market(ticker=ticker)
        m = getattr(resp, "market", resp)
        print(f"  yes_bid={m.yes_bid} yes_ask={m.yes_ask} last_price={m.last_price} volume={m.volume}")

    # Broader scan: check ALL open KXNFLGAME + KXNCAAFGAME markets for ANY with real volume
    print("\n=== Scanning all open game markets for ANY with volume > 0 ===")
    found_any = False
    for series in ["KXNFLGAME", "KXNCAAFGAME"]:
        resp = client.get_markets(series_ticker=series, status="open", limit=1000)
        markets = getattr(resp, "markets", None) or []
        for m in markets:
            vol = getattr(m, "volume", None)
            if vol and vol > 0:
                print(f"  {m.ticker}: volume={vol} yes_bid={getattr(m,'yes_bid',None)} yes_ask={getattr(m,'yes_ask',None)}")
                found_any = True

    if not found_any:
        print("  No markets found with any recorded volume at all.")
