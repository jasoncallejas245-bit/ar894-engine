import os
from kalshi_python import Configuration, KalshiClient

KEY_ID = os.environ["KALSHI_KEY_ID"]
PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]

with open(PRIVATE_KEY_PATH, "r") as f:
    private_key_content = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = KEY_ID
config.private_key_pem = private_key_content

TICKER = "KXNCAAFGAME-26SEP06WSUWASH-WASH"

with KalshiClient(config) as client:
    print(f"=== get_trades for {TICKER} ===")
    try:
        resp = client.get_trades(ticker=TICKER, limit=10)
        trades = getattr(resp, "trades", None) or []
        print(f"Trades found: {len(trades)}")
        for t in trades:
            print(f"  {t}")
    except Exception as e:
        print(f"get_trades failed: {e}")

    print(f"\n=== get_market_candlesticks for {TICKER} ===")
    try:
        resp = client.get_market_candlesticks(
            series_ticker="KXNCAAFGAME",
            ticker=TICKER,
            period_interval=60,
        )
        print(resp)
    except Exception as e:
        print(f"get_market_candlesticks failed: {e}")

    print(f"\n=== Re-checking get_market fresh (in case it just changed) ===")
    resp = client.get_market(ticker=TICKER)
    m = getattr(resp, "market", resp)
    print(f"  yes_bid={m.yes_bid} yes_ask={m.yes_ask} last_price={m.last_price} volume={m.volume} volume_24h={m.volume_24h} status={m.status}")
