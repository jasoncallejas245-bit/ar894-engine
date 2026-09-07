import os
import time
from pykalshi import KalshiClient, MarketStatus

os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])

client = KalshiClient()

print("Step 1: fetching BTC spot price...")
t0 = time.time()
import requests
resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
print(f"  done in {time.time()-t0:.2f}s -> {resp.json()}")

print("\nStep 2: querying Kalshi for KXBTC15M open markets...")
t0 = time.time()
try:
    markets = client.get_markets(series_ticker="KXBTC15M", status=MarketStatus.OPEN, limit=5)
    print(f"  done in {time.time()-t0:.2f}s -> found {len(markets)} markets")
    for m in markets[:3]:
        print(f"    {m.ticker}: {m.title}")
except Exception as e:
    print(f"  FAILED after {time.time()-t0:.2f}s: {e}")

print("\nDone.")
