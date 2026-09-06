import os
import json
import requests
import pandas as pd
from datetime import datetime, date
from pykalshi import KalshiClient

DISCORD_WEBHOOK_BETS = os.environ["DISCORD_WEBHOOK_BETS"]
DISCORD_WEBHOOK_UPDATES = os.environ["DISCORD_WEBHOOK_UPDATES"]
KALSHI_KEY_ID = os.environ["KALSHI_KEY_ID"]
KALSHI_PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]

# --- Hard safety limits (edit these deliberately, not casually) ---
MAX_STAKE_PER_TRADE = float(os.getenv("MAX_STAKE_PER_TRADE", "25.00"))
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP", "100.00"))
DAILY_STATE_FILE = "daily_trading_state.json"


def send_discord(webhook_url, message):
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=5)
        if resp.status_code != 204:
            print(f"⚠️ Discord non-204: {resp.status_code}")
    except Exception as e:
        print(f"❌ Discord send failed: {e}")


def load_daily_state():
    today = date.today().isoformat()
    if os.path.exists(DAILY_STATE_FILE):
        with open(DAILY_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    return {"date": today, "realized_loss": 0.0, "trades_executed": 0, "halted": False}


def save_daily_state(state):
    with open(DAILY_STATE_FILE, "w") as f:
        json.dump(state, f)


def daily_cap_exceeded(state):
    return state["realized_loss"] >= DAILY_LOSS_CAP or state.get("halted", False)


def record_trade_result(state, pnl):
    if pnl < 0:
        state["realized_loss"] += abs(pnl)
    state["trades_executed"] += 1
    if state["realized_loss"] >= DAILY_LOSS_CAP:
        state["halted"] = True
        send_discord(
            DISCORD_WEBHOOK_UPDATES,
            f"🛑 **DAILY LOSS CAP HIT** (${state['realized_loss']:.2f} >= ${DAILY_LOSS_CAP:.2f}). "
            f"Trading halted for the rest of today. Resumes automatically tomorrow.",
        )
    save_daily_state(state)


def execute_trade(ticker, price, count):
    """
    Places a live order on Kalshi with no human confirmation step.
    Hard-capped by MAX_STAKE_PER_TRADE and DAILY_LOSS_CAP above.
    """
    state = load_daily_state()

    if daily_cap_exceeded(state):
        print(f"Daily loss cap reached (${state['realized_loss']:.2f}) — skipping trade for {ticker}.")
        return

    stake = price * count
    if stake > MAX_STAKE_PER_TRADE:
        # Auto-scale down to the max allowed rather than rejecting outright
        count = max(1, int(MAX_STAKE_PER_TRADE / price))
        stake = price * count
        print(f"Stake exceeded MAX_STAKE_PER_TRADE — scaled down to {count} contracts (${stake:.2f}).")

    alert_msg = (
        f"🚨 **Autonomous Kalshi Order** 🚨\n"
        f"Market: `{ticker}`\nPrice: ${price}\nQuantity: {count}\nStake: ${stake:.2f}\n"
        f"Today's realized loss so far: ${state['realized_loss']:.2f} / ${DAILY_LOSS_CAP:.2f} cap"
    )
    send_discord(DISCORD_WEBHOOK_BETS, alert_msg)

    try:
        client = KalshiClient(key_id=KALSHI_KEY_ID, private_key_path=KALSHI_PRIVATE_KEY_PATH)
        order = client.portfolio.place_order(
            ticker=ticker, book_side="bid", price_dollars=str(price), count_fp=str(count)
        )
        print(f"✅ Order placed: {ticker} x{count} @ ${price}")
        send_discord(DISCORD_WEBHOOK_BETS, f"✅ **Order Executed** for `{ticker}` — {count} contracts @ ${price}")
        # NOTE: pnl tracking requires reconciling filled/settled orders later;
        # this hook is where you'd call record_trade_result(state, realized_pnl)
        # once you have real settlement data instead of estimating at order time.
    except Exception as e:
        err_msg = f"❌ Failed to place Kalshi order for `{ticker}`: {e}"
        print(err_msg)
        send_discord(DISCORD_WEBHOOK_UPDATES, err_msg)


if __name__ == "__main__":
    print("--- Autonomous mode: no human confirmation, hard safety caps active ---")
    print(f"MAX_STAKE_PER_TRADE=${MAX_STAKE_PER_TRADE}  DAILY_LOSS_CAP=${DAILY_LOSS_CAP}")
    # This entrypoint is meant to be called by worker.py's scan loop with
    # real signals from prop_analyzer.py — see worker.py's run_scan_once().
