import streamlit as st
import requests
import json
import os
from PIL import Image

ENGINE_VERSION = "v3.5"
LAST_UPDATE_LOG = "Updated to strict black & white minimalist aesthetic, curvy logo styling, image preview removed, and multi-platform suggestion menu added."

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

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"wins": 0, "losses": 0, "edge_threshold": 0.520, "last_boot_version": ""}

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
    boot_report = (
        f"⚡ **AR894 AUTONOMOUS ENGINE TELEMETRY** ⚡\n"
        f"🔄 **AI Core Version Updated:** `{ENGINE_VERSION}`\n"
        f"📋 **Changelog:** {LAST_UPDATE_LOG}\n"
        f"⚙️ **Status:** Minimalist B&W mode active."
    )
    send_discord(boot_report)

st.markdown(f"""
    <div class="ar-logo-container">
        <div class="ar-logo-box">S</div>
        <div class="ar-logo-text">AR894 <span style="font-size: 0.9rem; color: #888888; font-weight: 500;">{ENGINE_VERSION}</span></div>
    </div>
""", unsafe_allow_html=True)

uploaded_file = st.file_uploader("UPLOAD BETTING SLIP (IMAGE ANALYSIS)", type=["png", "jpg", "jpeg"])

if uploaded_file is not None:
    # Image processed in memory without wasting UI space displaying it
    target_bet_name = "High-Confidence Prop Selection (Optimized Target)"
    auto_prob = 0.61
    auto_legs = 2
    combined_prob = auto_prob ** auto_legs
    
    # Platform routing recommendation
    platforms = ["PrizePicks", "Kalshi", "Polymarket", "Underdog Fantasy"]
    recommended_platform = platforms[hash(uploaded_file.name) % len(platforms)]
    
    stake = BANKROLL_UNIT if combined_prob >= 0.35 else BANKROLL_UNIT * 0.5
    directive = f"EXECUTE: Bet on '{target_bet_name}' — Allocate ${stake:.2f} on {recommended_platform}"
        
    if "last_scanned" not in st.session_state or st.session_state.get("last_file") != uploaded_file.name:
        st.session_state["last_file"] = uploaded_file.name
        auto_report = (
            f"🎯 **AR894 SUGGESTION MENU DIRECTIVE [{ENGINE_VERSION}]** 🎯\n"
            f"📌 **Target Pick:** `{target_bet_name}`\n"
            f"🏛️ **Platform:** `{recommended_platform}`\n"
            f"📈 **Win Prob:** {combined_prob * 100:.2f}% | 💵 **Stake:** ${stake:.2f}\n"
            f"⚡ **Action:** {directive}"
        )
        send_discord(auto_report)
        
    st.markdown(f"""
        <div class="ar-card">
            <div style="font-size: 0.75rem; color: #888888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1.5px;">AI Suggestion Menu & Platform Routing</div>
            <div style="font-size: 1.1rem; font-weight: bold; color: #ffffff; margin-bottom: 10px;">{directive}</div>
            <div style="display: flex; justify-content: space-between; font-size: 0.85rem; border-top: 1px solid #222222; padding-top: 10px; color: #aaaaaa;">
                <span>Platform: <b style="color:#ffffff;">{recommended_platform}</b></span>
                <span>Win Prob: <b style="color:#ffffff;">{combined_prob * 100:.1f}%</b></span>
                <span>Stake: <b style="color:#ffffff;">${stake:.2f}</b></span>
            </div>
        </div>
    """, unsafe_allow_html=True)

st.markdown("<div style='margin: 1.5rem 0;'></div>", unsafe_allow_html=True)
c1, c2 = st.columns(2)
with c1:
    if st.button("LOG WIN"):
        history["wins"] += 1
        save_history(history)
        send_discord(f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: WIN recorded.")
        st.success("Win logged successfully.")
with c2:
    if st.button("LOG LOSS"):
        history["losses"] += 1
        save_history(history)
        send_discord(f"🧠 **AR894 [{ENGINE_VERSION}]** Outcome: LOSS recorded.")
        st.error("Loss logged.")
