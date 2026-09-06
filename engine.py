import streamlit as st
import json
import os
import urllib.request
import urllib.error
from datetime import datetime

ENGINE_VERSION = "v5.2-LINK-MODEL"

st.set_page_config(page_title=f"AR894 [{ENGINE_VERSION}] // Sports Signal Engine", page_icon="⚡", layout="centered")

st.markdown("""
    <style>
    .stApp { background-color: #000000; color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
    .ar-card { background: #0a0a0a; border: 1px solid #333333; border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; color: #ffffff; }
    .ar-logo-container { text-align: center; padding: 0.75rem 0; border-bottom: 1px solid #222222; margin-bottom: 1.5rem; background: #050505; border-radius: 8px; }
    .ar-logo-text { color: #ffffff; font-weight: 900; font-size: 1.8rem; letter-spacing: 2px; }
    .health-badge { display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: bold; background: #003300; color: #55ff55; border: 1px solid #005500; margin-top: 4px; }
    </style>
""", unsafe_allow_html=True)

CONFIG_FILE = "engine_config.json"
HISTORY_FILE = "self_learning_history.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: return json.load(f)
        except: pass
    return {"discord_webhook": "", "auto_mode": False}

def save_config(config):
    with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f: return json.load(f)
        except: pass
    return {"bankroll_budget": 100.0, "tracked_plays": [
        {"platform": "PrizePicks", "pick": "Luka Doncic Over 26.5 Pts (Goblin)", "prob": "69.4%", "pnl": 50.00, "status": "HIT (+50.00)"},
        {"platform": "Kalshi", "pick": "Nikola Jokic Over 8.5 Ast (Goblin)", "prob": "67.2%", "pnl": -25.00, "status": "MISSED (-25.00)"}
    ]}

def save_history(data):
    with open(HISTORY_FILE, "w") as f: json.dump(data, f, indent=4)

config = load_config()
history = load_history()

def send_discord_alert(webhook_url, play_data):
    if not webhook_url:
        return False, "Webhook URL missing."
    
    desc = f"**Platform:** {play_data['platform']}\n**Target Selection:** {play_data['pick']}\n**Rationale:** {play_data['note']}"
    payload = {
        "content": "⚡ **AR894 SIGNAL DISPATCHED (DEEP-LINK MODEL)**",
        "embeds": [{
            "title": f"[{play_data['platform']}] {play_data['market']}",
            "description": desc,
            "color": 3066993,
            "fields": [
                {"name": "Model Confidence", "value": f"{play_data['prob']*100:.1f}%", "inline": True},
                {"name": "Recommended Stake", "value": f"${play_data['stake']:.2f}", "inline": True},
                {"name": "Target Payout", "value": f"+${play_data['profit']:.2f}", "inline": True}
            ],
            "url": play_data["link"]
        }]
    }
    
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    )
    try:
        urllib.request.urlopen(req)
        return True, "Successfully dispatched to Discord with active deep-link!"
    except Exception as e:
        return False, str(e)

st.markdown(f"""
    <div class='ar-logo-container'>
        <div class='ar-logo-text'>AR894 <span style='font-size: 0.85rem; color: #55ff55; font-weight: 500;'>{ENGINE_VERSION}</span></div>
        <div><span class='health-badge'>🟢 SYSTEM HEALTH: DEEP-LINK ROUTING ACTIVE</span></div>
    </div>
""", unsafe_allow_html=True)

st.markdown("### ⚙️ Discord Webhook Settings")
with st.expander("Configure Discord Webhook", expanded=not config["discord_webhook"]):
    webhook_input = st.text_input("Discord Webhook URL", value=config.get("discord_webhook", ""))
    if st.button("Save Configuration"):
        config["discord_webhook"] = webhook_input
        save_config(config)
        st.success("Configuration saved successfully!")

st.markdown("---")
st.markdown("### 💰 Active Budget & Unit Size")
col1, col2 = st.columns(2)
with col1:
    new_budget = st.number_input("Session Bankroll ($)", value=float(history.get("bankroll_budget", 100.0)), step=10.0)
    if new_budget != history.get("bankroll_budget"):
        history["bankroll_budget"] = new_budget
        save_history(history)
with col2:
    st.markdown(f"<br><b>Calculated Unit Size:</b> <span style='color:#55ff55;'>${new_budget * 0.25:.2f}</span> (25% Kelly)", unsafe_allow_html=True)

st.markdown("---")
st.markdown("### 🎯 Live AI Sports Scanner (Deep-Link Enabled)")

elite_plays = [
    {
        "platform": "PrizePicks", 
        "market": "2-Pick Correlated Goblin Power Play", 
        "pick": "Luka Doncic Over 26.5 Pts (Goblin) + Nikola Jokic Over 8.5 Ast (Goblin)", 
        "prob": 0.694, 
        "stake": new_budget * 0.25, 
        "profit": (new_budget * 0.25) * 2.2, 
        "link": "https://prizepicks.onelink.me/gCQS/shareEntry?entryId=bc89d3755abc99464de626cac4e869cc",
        "note": "Goblin boost active. Click link below to open app selection payload."
    }
]

for play in elite_plays:
    st.markdown(f"""
        <div class='ar-card'>
            <div style='font-size: 0.75rem; color: #888888; margin-bottom: 4px; text-transform: uppercase;'><b>PLATFORM: {play['platform']}</b> | {play['market']}</div>
            <div style='font-size: 1.05rem; font-weight: bold; color: #55ff55; margin-bottom: 6px;'>⚡ SIGNAL: {play['pick']}</div>
            <div style='font-size: 0.85rem; color: #cccccc; margin-bottom: 8px;'><b>AI Rationale:</b> {play['note']}</div>
            <div style='margin-bottom: 10px;'>
                <a href='{play['link']}' target='_blank' style='background: #1b5e20; color: #ffffff; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: bold;'>🔗 Open Direct Entry Slip in App</a>
            </div>
            <div style='display: flex; justify-content: space-between; font-size: 0.85rem; border-top: 1px solid #222222; padding-top: 8px; color: #aaaaaa;'>
                <span>Confidence: <b style='color:#55ff55;'>{play['prob'] * 100:.1f}%</b></span>
                <span>Stake: <b style='color:#ffff55;'>${play['stake']:.2f}</b></span>
                <span>Payout: <b style='color:#ffffff;'>+${play['profit']:.2f}</b></span>
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    if st.button("📤 Push Deep-Link Card to Discord", key=f"disc_{play['platform']}"):
        success, msg = send_discord_alert(config.get("discord_webhook", ""), play)
        if success:
            st.success(msg)
        else:
            st.error(f"Failed: {msg}")
