import requests

TICKER = "KXNCAAFGAME-26SEP06WSUWASH-WASH"
BASE = "https://api.elections.kalshi.com/trade-api/v2"

print(f"=== Raw GET /markets/{TICKER}/trades ===")
resp = requests.get(f"{BASE}/markets/trades", params={"ticker": TICKER, "limit": 5})
print(f"Status: {resp.status_code}")
print(resp.text[:2000])

print(f"\n=== Raw GET /markets/{TICKER} ===")
resp = requests.get(f"{BASE}/markets/{TICKER}")
print(f"Status: {resp.status_code}")
print(resp.text[:2000])
