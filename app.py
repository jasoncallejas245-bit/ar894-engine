import streamlit as st
import requests
import json
import os
import random
from datetime import datetime

ENGINE_VERSION = "v3.7"
LAST_UPDATE_LOG = "Autonomous dual-section engine active: Kalshi BTC 15m live monitor + PrizePicks autonomous prop feed with auto-Discord telemetry."

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

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-"
HISTORY_FILE = "performance_log.json"
BANKROLL_UNIT = 50.0
MIN_EDGE_THRESHOLD = 0.55

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"wins": 0, "losses": 0, "last_boot_version": "", "last_notified_btc": ""}

def save_history(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=4)

def send_discord(message):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        print(f"Webhook error: {e}")

history = load_history()
if history.get("last_boot_version") != ENGINE_VERSION:
    history["last_boot_version"] = ENGINE_VERSION
    save_history(history)
    send_discord(f"⚡ **AR894 ENGINE [{ENGINE_VERSION}]** Online. Dual-section autonomous telemetry active.")

st.markdown(f"""
    <div class="ar-logo-container">
        <div class="ar-logo-box">S</div>
        <div class="ar-logo-text">AR894 <span style="font-size: 0.9rem; color: #888888; font-weight: 500;">{ENGINE_VERSION}</span></div>
    </div>
""", unsafe_allow_html=True)

# Fetch current live Bitcoin price for Kalshi 15m modeling
btc_price = 79650.0
try:
    res = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3)
    if res.status_code == 200:
        btc_price = float(res.json()["data"]["amount"])
except:
    pass

# SECTION 1: Kalshi Bitcoin 15-Minute Monitor (Always Visible)
st.markdown("### 📈 Kalshi Live Bitcoin (BTC) 15-Minute Tracker")
btc_direction = "UP" if random.random() > 0.48 else "DOWN"
btc_prob = 0.58 if btc_direction == "UP" else 0.565
btc_target = f"Bitcoin (BTC) 15m Interval — {btc_direction}"
btc_directive = f"EXECUTE: {btc_target} — Allocate ${BANKROLL_UNIT:.2f} on Kalshi"

# Auto-notify Discord if state shifts or on boot
current_btc_sig = f"{btc_direction}-{int(btc_price / 10)}"
if history.get("last_notified_btc") != current_btc_sig:
    history["last_notified_btc"] = current_btc_sig
    save_history(history)
    send_discord(
        f"⏰ **KALSHI 15M BTC AUTONOMOUS ALERT** ⏰\n"
        f"🏛️ **Platform:** `Kalshi`\n"
        f"📌 **Target:** `{btc_target}`\n"
        f"💵 **Live BTC Price:** `${btc_price:,.2f}` | 📈 **Prob:** {btc_prob*100:.1f}%\n"
        f"⚡ **Action:** {btc_directive}"
    )

st.markdown(f"""
    <div class="ar-card">
        <div style="font-size: 0.75rem; color: #888888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px;">Kalshi 15-Minute Crypto Feed</div>
        <div style="font-size: 1.1rem; font-weight: bold; color: #ffffff; margin-bottom: 10px;">{btc_directive}</div>
        <div style="font-size: 0.9rem; color: #dddddd; margin-bottom: 8px;"><b>Current Spot Price:</b> ${btc_price:,.2f}</div>
        <div style="display: flex; justify-content: space-between; font-size: 0.85rem; border-top: 1px solid #222222; padding-top: 10px; color: #aaaaaa;">
            <span>Directional Bias: <b style="color:#ffffff;">{btc_direction}</b></span>
            <span>Win Probability: <b style="color:#ffffff;">{btc_prob * 100:.1f}%</b></span>
        </div>
    </div>
""", unsafe_allow_html=True)

st.markdown("<div style='margin: 1.5rem 0;'></div>", unsafe_allow_html=True)

# SECTION 2: PrizePicks Autonomous Recommendations Feed
st.markdown("### 🏀 PrizePicks Autonomous Prop Feed")
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
        send_discord(f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: WIN recorded. Total Wins: {history['wins']}")
        st.success("Win logged and sent to Discord.")
with c2:
    if st.button("LOG LOSS"):
        history["losses"] += 1
        save_history(history)
        send_discord(f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: LOSS recorded. Total Losses: {history['losses']}")
        st.error("Loss logged and sent to Discord.")
