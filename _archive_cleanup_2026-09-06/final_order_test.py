import os
from pykalshi import KalshiClient, Action, Side, MarketStatus

os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])

client = KalshiClient()

TICKER = "KXNCAAFGAME-26SEP06WSUWASH-WASH"

print(f"=== Checking live price for {TICKER} ===")
market = client.get_market(TICKER)
yes_ask = getattr(market, "yes_ask_dollars", None)
print(f"Title: {market.title}")
print(f"Current yes_ask: ${yes_ask}")

if not yes_ask:
    print("No live price available right now — cannot test.")
else:
    yes_ask = float(yes_ask)
    # Price slightly above the current ask so the order crosses the book
    # and fills immediately, like a marketable limit order.
    test_price = min(0.99, round(yes_ask + 0.01, 2))
    count_fp = "1"
    stake = test_price * 1

    print(f"\nAbout to place a REAL order: BUY 1 YES contract on '{market.title}'")
    print(f"Limit price: ${test_price:.2f}  Estimated stake: ${stake:.2f}")
    confirm = input("Type YES to proceed: ")

    if confirm.strip().upper() != "YES":
        print("Cancelled — no order placed.")
    else:
        try:
            order = client.portfolio.place_order(
                TICKER,
                Action.BUY,
                Side.YES,
                count_fp=count_fp,
                yes_price_dollars=f"{test_price:.2f}",
            )
            print("\n✅ ORDER RESULT:")
            print(order)
        except Exception as e:
            print(f"\n❌ ORDER FAILED: {e}")
