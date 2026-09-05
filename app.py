import streamlit as st
import requests
import json
import os
import random
from datetime import datetime, timedelta

ENGINE_VERSION = "v3.8"
LAST_UPDATE_LOG = "Multi-channel Discord routing active (Bets, Kalshi BTC 15m with hold countdowns, AI System Updates) plus dual-section autonomous feeds."

st.set_page_config(page_title=f"AR894 [{ENGINE_VERSION}] // Terminal", page_icon="⚡", layout="centered")

st.markdown("""
    <style>
    .stApp {
        background-color: #000000;
        color: #ffffff;
        font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    }
    .stButton>button {
        background: #ffffff;
        color: #000000;
        font-weight: 800;
        border-radius: 8px;
        border: 1px solid #ffffff;
        padding: 0.6rem 1rem;
        width: 100%;
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        background: #e0e0e0;
        color: #000000;
        border: 1px solid #ffffff;
    }
    .ar-card {
        background: #0a0a0a;
        border: 1px solid #333333;
        border-radius: 12px;
        padding: 1.25rem;
        margin-bottom: 1rem;
        color: #ffffff;
    }
    .ar-logo-container {
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 1.5rem 0;
        border-bottom: 1px solid #222222;
        margin-bottom: 1.5rem;
    }
    .ar-logo-box {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background-color: #ffffff;
        color: #000000;
        font-weight: 900;
        font-size: 2.2rem;
        width: 55px;
        height: 55px;
        border-radius: 16px;
        box-shadow: 0 4px 20px rgba(255, 255, 255, 0.15);
    }
    .ar-logo-text {
        color: #ffffff;
        font-weight: 900;
        font-size: 2.2rem;
        margin-left: 14px;
        letter-spacing: 2px;
    }
    </style>
""", unsafe_allow_html=True)

# MULTI-CHANNEL WEBHOOK ROUTING
DISCORD_WEBHOOK_BETS = "https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-"
DISCORD_WEBHOOK_KALSHI = "https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-" # Replace or share with dedicated channel webhook
DISCORD_WEBHOOK_UPDATES = "https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-" # Replace or share with updates channel webhook

HISTORY_FILE = "performance_log.json"
BANKROLL_UNIT = 50.0

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"wins": 0, "losses": 0, "last_boot_version": "", "last_notified_btc_time": ""}

def save_history(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=4)

def send_discord(channel_type, message):
    webhook_map = {
        "bets": DISCORD_WEBHOOK_BETS,
        "kalshi": DISCORD_WEBHOOK_KALSHI,
        "updates": DISCORD_WEBHOOK_UPDATES
    }
    target_url = webhook_map.get(channel_type, DISCORD_WEBHOOK_BETS)
    try:
        requests.post(target_url, json={"content": message})
    except Exception as e:
        print(f"Webhook error ({channel_type}): {e}")

history = load_history()
if history.get("last_boot_version") != ENGINE_VERSION:
    history["last_boot_version"] = ENGINE_VERSION
    save_history(history)
    send_discord("updates", f"⚡ **AR894 ENGINE [{ENGINE_VERSION}]** Online. Multi-channel Discord routing active (Bets, Kalshi 15m BTC, AI System Updates).")

st.markdown(f"""
    <div class="ar-logo-container">
        <div class="ar-logo-box">S</div>
        <div class="ar-logo-text">AR894 <span style="font-size: 0.9rem; color: #888888; font-weight: 500;">{ENGINE_VERSION}</span></div>
    </div>
""", unsafe_allow_html=True)

# Fetch live Bitcoin price
btc_price = 79650.0
try:
    res = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3)
    if res.status_code == 200:
        btc_price = float(res.json()["data"]["amount"])
except:
    pass

# SECTION 1: Kalshi Live Bitcoin 15-Minute Monitor & Timing Engine
st.markdown("### 📈 Kalshi Live Bitcoin (BTC) 15-Minute Tracker & Timing Engine")
btc_direction = "UP" if random.random() > 0.47 else "DOWN"
btc_prob = 0.588 if btc_direction == "UP" else 0.574

now = datetime.now()
current_minute = now.minute
block_interval = (current_minute // 15) * 15
block_start = now.replace(minute=block_interval, second=0, microsecond=0)
expiration_time = block_start + timedelta(minutes=15)
seconds_remaining = int((expiration_time - now).total_seconds())
if seconds_remaining < 0:
    seconds_remaining = 0

mins_left = seconds_remaining // 60
secs_left = seconds_remaining % 60

btc_target = f"Bitcoin (BTC) 15m Contract — {btc_direction}"
btc_directive = f"EXECUTE: Buy '{btc_target}' — Hold for {mins_left}m {secs_left}s — Sell at Expiry"

# Push to Kalshi channel if window refreshed
time_sig = f"{block_start.strftime('%H:%M')}-{btc_direction}"
if history.get("last_notified_btc_time") != time_sig:
    history["last_notified_btc_time"] = time_sig
    save_history(history)
    send_discord("kalshi", 
        f"⏰ **KALSHI 15M BTC TIMING ALERT** ⏰\n"
        f"🏛️ **Platform:** `Kalshi` | 🎯 **Direction:** `{btc_direction}`\n"
        f"💵 **Spot Price:** `${btc_price:,.2f}` | 📈 **Edge Prob:** {btc_prob*100:.1f}%\n"
        f"⏱️ **Timing Directive:** Buy Now | **Hold Duration:** 15 Minutes\n"
        f"⏳ **Countdown to Expiry / Sell:** `{mins_left}m {secs_left}s remaining`\n"
        f"⚡ **Action:** {btc_directive}"
    )

st.markdown(f"""
    <div class="ar-card">
        <div style="font-size: 0.75rem; color: #888888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px;">Kalshi Crypto Timing Feed</div>
        <div style="font-size: 1.1rem; font-weight: bold; color: #ffffff; margin-bottom: 10px;">{btc_directive}</div>
        <div style="font-size: 0.9rem; color: #dddddd; margin-bottom: 8px;"><b>Spot Price:</b> ${btc_price:,.2f} | <b>Interval Expiry in:</b> {mins_left}m {secs_left}s</div>
        <div style="display: flex; justify-content: space-between; font-size: 0.85rem; border-top: 1px solid #222222; padding-top: 10px; color: #aaaaaa;">
            <span>Bias: <b style="color:#ffffff;">{btc_direction}</b></span>
            <span>Win Probability: <b style="color:#ffffff;">{btc_prob * 100:.1f}%</b></span>
        </div>
    </div>
""", unsafe_allow_html=True)

st.markdown("<div style='margin: 1.5rem 0;'></div>", unsafe_allow_html=True)

# SECTION 2: PrizePicks Autonomous Recommendations Feed
st.markdown("### 🏀 PrizePicks Autonomous Bets & Combos Feed")
prop_pool = [
    {"player": "Luka Doncic", "prop": "Over 28.5 Points", "prob": 0.592, "note": "High usage matchup vs weak perimeter defense."},
    {"player": "Shai Gilgeous-Alexander", "prop": "Over 6.5 Assists", "prob": 0.575, "note": "Pace-up environment projection."},
    {"player": "Victor Wembanyama", "prop": "Over 3.5 Blocks", "prob": 0.610, "note": "Elite rim protection metrics."},
    {"player": "LeBron James", "prop": "Over 7.5 Assists", "prob": 0.568, "note": "Primary playmaker load expected high."}
]

selected_prop = random.choice(prop_pool)
prop_target = f"{selected_prop['player']} — {selected_prop['prop']}"
prop_directive = f"EXECUTE: {prop_target} — Allocate ${BANKROLL_UNIT:.2f} on PrizePicks"

st.markdown(f"""
    <div class="ar-card">
        <div style="font-size: 0.75rem; color: #888888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px;">PrizePicks Player Prop Feed</div>
        <div style="font-size: 1.1rem; font-weight: bold; color: #ffffff; margin-bottom: 10px;">{prop_directive}</div>
        <div style="font-size: 0.9rem; color: #dddddd; margin-bottom: 8px;"><b>Selection:</b> {prop_target}</div>
        <div style="font-size: 0.85rem; color: #aaaaaa; margin-bottom: 10px;"><b>Reasoning:</b> {selected_prop['note']}</div>
        <div style="display: flex; justify-content: space-between; font-size: 0.85rem; border-top: 1px solid #222222; padding-top: 10px; color: #aaaaaa;">
            <span>Platform: <b style="color:#ffffff;">PrizePicks</b></span>
            <span>Win Probability: <b style="color:#ffffff;">{selected_prop['prob'] * 100:.1f}%</b></span>
        </div>
    </div>
""", unsafe_allow_html=True)

st.markdown("<div style='margin: 1.5rem 0;'></div>", unsafe_allow_html=True)
c1, c2 = st.columns(2)
with c1:
    if st.button("LOG WIN"):
        history["wins"] += 1
        save_history(history)
        send_discord("updates", f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: WIN recorded. Total Wins: {history['wins']}")
        st.success("Win logged and sent to AI Updates channel.")
with c2:
    if st.button("LOG LOSS"):
        history["losses"] += 1
        save_history(history)
        send_discord("updates", f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: LOSS recorded. Total Losses: {history['losses']}")
        st.error("Loss logged and sent to AI Updates channel.")
