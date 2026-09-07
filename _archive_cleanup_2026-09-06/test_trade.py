import os
import uuid
from kalshi_python import Configuration, KalshiClient
from kalshi_python.models import CreateOrderRequest

KEY_ID = os.environ["KALSHI_KEY_ID"]
PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]

with open(PRIVATE_KEY_PATH, "r") as f:
    private_key_content = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = KEY_ID
config.private_key_pem = private_key_content

TEST_STAKE_CENTS = 100  # $1.00 hard cap for this test

with KalshiClient(config) as client:
    print("=== Gathering open NFL + NCAAF game markets, soonest first ===")
    all_markets = []
    for series in ["KXNFLGAME", "KXNCAAFGAME"]:
        resp = client.get_markets(series_ticker=series, status="open", limit=1000)
        markets = getattr(resp, "markets", None) or []
        all_markets.extend(markets)
        print(f"{series}: {len(markets)} open markets")

    # Sort by close_time ascending — soonest games most likely to have real liquidity
    all_markets.sort(key=lambda m: getattr(m, "close_time", None) or "9999")

    print(f"\nTotal candidates: {len(all_markets)}")
    print("Checking soonest-closing markets first for live pricing...\n")

    target = None
    target_price = None
    checked = 0

    for m in all_markets[:30]:  # only check the 30 soonest to limit API calls
        checked += 1
        try:
            detail_resp = client.get_market(ticker=m.ticker)
            detail = getattr(detail_resp, "market", detail_resp)
            yes_ask = getattr(detail, "yes_ask", None)
            print(f"  [{checked}] {m.ticker} ({m.title}) close={m.close_time} yes_ask={yes_ask}")
            if yes_ask:
                target = m
                target_price = yes_ask
                break
        except Exception as e:
            print(f"  [{checked}] {m.ticker} failed: {e}")

    if not target:
        print("\nStill no live pricing found in the soonest 30 markets.")
    else:
        print(f"\nFound live market: {target.ticker} ({target.title}) at yes_ask={target_price}c")

        confirm = input(f"\nAbout to place a REAL ${TEST_STAKE_CENTS/100:.2f} test order on {target.ticker}. Type YES to proceed: ")
        if confirm.strip().upper() != "YES":
            print("Cancelled — no order placed.")
        else:
            order_request = CreateOrderRequest(
                ticker=target.ticker,
                client_order_id=str(uuid.uuid4()),
                side="yes",
                action="buy",
                count=1,
                type="market",
                buy_max_cost=TEST_STAKE_CENTS,
            )
            try:
                result = client.create_order(create_order_request=order_request)
                print("\n✅ ORDER RESULT:")
                print(result)
            except Exception as e:
                print(f"\n❌ ORDER FAILED: {e}")
