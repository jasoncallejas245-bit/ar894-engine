import os

os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])

from pykalshi import KalshiClient

client = KalshiClient()

print("=== Balance ===")
balance = client.portfolio.get_balance()
print(balance)

print("\n=== Market detail with real dollar pricing ===")
market = client.get_market("KXNCAAFGAME-26SEP06WSUWASH-WASH")
print(f"Title: {market.title}")
print(f"yes_bid_dollars: {getattr(market, 'yes_bid_dollars', 'N/A')}")
print(f"yes_ask_dollars: {getattr(market, 'yes_ask_dollars', 'N/A')}")
print(f"volume_fp: {getattr(market, 'volume_fp', 'N/A')}")
