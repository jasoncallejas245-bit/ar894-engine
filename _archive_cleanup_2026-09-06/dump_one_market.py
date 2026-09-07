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
    print(f"=== get_market('{TICKER}') full dump ===")
    resp = client.get_market(ticker=TICKER)
    m = getattr(resp, "market", resp)
    if hasattr(m, "model_dump"):
        for k, v in m.model_dump().items():
            print(f"  {k}: {v}")
    else:
        print(vars(m))

    print(f"\n=== get_market_orderbook('{TICKER}') full dump ===")
    try:
        ob = client.get_market_orderbook(ticker=TICKER)
        if hasattr(ob, "model_dump"):
            print(ob.model_dump())
        else:
            print(vars(ob))
    except Exception as e:
        print(f"Orderbook fetch failed: {e}")
