import os
import requests
import pandas as pd
from pykalshi import KalshiClient

DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-'
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "YOUR_ACTUAL_KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "/path/to/your/kalshi_private_key.pem")

def send_discord_alert(message):
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        if response.status_code == 204:
            print("✅ Discord alert sent successfully!")
        else:
            print(f"⚠️ Failed to send Discord alert, status code: {response.status_code}")
    except Exception as e:
        print(f"❌ Error sending Discord alert: {e}")

def analyze_betting_history():
    try:
        df = pd.read_csv('transaction_log.csv')
        total_deposits = df[df['Type'] == 'Deposit']['Amount'].sum()
        total_withdrawals = df[df['Type'] == 'Withdrawn']['Amount'].sum()
        lineups_placed = len(df[df['Type'] == 'Lineup Placed'])
        lineups_won = len(df[df['Type'] == 'Lineup Won'])
        win_rate = (lineups_won / lineups_placed * 100) if lineups_placed > 0 else 0
        net_flow = total_deposits - total_withdrawals
        
        print("--- Betting History Analysis ---")
        print(f"Total Lineups Placed: {lineups_placed}")
        print(f"Total Lineups Won: {lineups_won} ({win_rate:.1f}% win rate)")
        print(f"Net Cash Flow: ${net_flow:.2f}")
        return {"lineups_placed": lineups_placed, "lineups_won": lineups_won, "win_rate": win_rate, "net_flow": net_flow}
    except Exception as e:
        print(f"Could not analyze transaction_log.csv: {e}")
        return None

def propose_and_execute_trade(ticker, price, count):
    stats = analyze_betting_history()
    stats_text = f"\n📊 *Stats: {stats['lineups_won']}/{stats['lineups_placed']} Wins ({stats['win_rate']:.1f}%)*" if stats else ""
    
    alert_msg = f"🚨 **New Kalshi Trade Proposal** 🚨\nMarket: `{ticker}`\nPrice: ${price}\nQuantity: {count}{stats_text}\n\n*Check your Mac terminal to confirm or cancel!*"
    send_discord_alert(alert_msg)
    
    confirmation = input(f"\nConfirm order for {ticker} ({count} contracts at ${price})? Type 'yes' to execute: ")
    if confirmation.strip().lower() == 'yes':
        print("Connecting to Kalshi API...")
        try:
            client = KalshiClient(key_id=KALSHI_KEY_ID, private_key_path=KALSHI_PRIVATE_KEY_PATH)
            order = client.portfolio.place_order(ticker=ticker, book_side="bid", price_dollars=str(price), count_fp=str(count))
            print("✅ Order successfully placed on Kalshi!")
            send_discord_alert(f"✅ **Order Successfully Executed** for `{ticker}`!")
        except Exception as e:
            err_msg = f"❌ Failed to place Kalshi order: {e}"
            print(err_msg)
            send_discord_alert(err_msg)
    else:
        print("Trade cancelled by user in terminal.")
        send_discord_alert("❌ Trade proposal cancelled by user.")

if __name__ == "__main__":
    propose_and_execute_trade(ticker="KXBTC-25MAR15-B100000", price=0.10, count=5)
