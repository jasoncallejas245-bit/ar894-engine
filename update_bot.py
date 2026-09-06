import os
import subprocess
import requests

DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL", 
    "https://discord.com/api/webhooks/1545665615300399134/wjXRYEOxerWH6Rd7QnOoLJeCE-gxFq2LG2V5Vwqo3YpaHsmIgO-3akGJiEX69XwB4wC-"
)

def git_commit_and_push(commit_message):
    try:
        print("📦 Staging changes...")
        subprocess.run(["git", "add", "."], check=True)
        
        print("📝 Committing changes...")
        subprocess.run(["git", "commit", "-m", commit_message], check=True)
        
        print("🚀 Pushing to GitHub (triggering Streamlit Cloud build)...")
        subprocess.run(["git", "push"], check=True)
        
        print("✅ Successfully pushed updates to GitHub!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Git push failed: {e}")
        return False

def notify_discord_update(commit_message):
    payload = {
        "content": f"🚀 **New Code Update Deployed!**\n> **Changelog:** `{commit_message}`\n\n*Streamlit Cloud is auto-rebuilding the live app now.*"
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if response.status_code == 204:
            print("✅ Discord update notification sent to channel!")
        else:
            print(f"⚠️ Failed to send Discord alert, status: {response.status_code}")
    except Exception as e:
        print(f"❌ Error sending Discord alert: {e}")

if __name__ == "__main__":
    print("--- 🤖 Project Auto-Updater ---")
    msg = input("Enter a description for this code update: ").strip()
    if msg:
        if git_commit_and_push(msg):
            notify_discord_update(msg)
    else:
        print("❌ Update cancelled: Description cannot be empty.")
