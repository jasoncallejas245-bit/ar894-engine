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
    print("=== Available public methods on KalshiClient ===")
    methods = [m for m in dir(client) if not m.startswith("_")]
    for m in sorted(methods):
        print(f"  {m}")
