import os
import shutil
from datetime import date

ARCHIVE = f"_archive_cleanup_{date.today().isoformat()}"
os.makedirs(ARCHIVE, exist_ok=True)

# --- 1. Trim requirements.txt to only what's actually imported now ---
# worker.py / paper_trading.py / dashboard.py only need: pykalshi, sharpapi
# (for the odds client used elsewhere), flask, requests. Streamlit and its
# huge dependency tree are dead weight now that engine.py is retired.
new_requirements = "pykalshi\nsharpapi\nflask\nrequests\n"
if os.path.exists("requirements.txt"):
    shutil.copy("requirements.txt", os.path.join(ARCHIVE, "requirements.txt.old"))
with open("requirements.txt", "w") as f:
    f.write(new_requirements)
print("requirements.txt trimmed to: pykalshi, sharpapi, flask, requests")

# --- 2. Archive obsolete/superseded files (not deleted, just moved aside) ---
obsolete_files = [
    "engine.py",           # old Streamlit UI, superseded by dashboard.py
    "prop_analyzer.py",    # old player-prop scanner, unused by worker.py
    "run_all.py",          # old standalone trade executor, superseded by worker.py
    "AR894.txt",
    "packages.txt",        # Streamlit-Cloud specific, unused on Railway
    "runtime.txt",         # Streamlit-Cloud specific, unused on Railway
    "run_ar894.command",
]
for fname in obsolete_files:
    if os.path.exists(fname):
        shutil.move(fname, os.path.join(ARCHIVE, fname))
        print(f"archived: {fname}")

# --- 3. Remove stale local state files (source of truth is now the Railway volume) ---
stale_state_files = [
    "daily_trading_state.json", "seen_trades.json", "open_positions.json",
    "paper_trades.json", "trade_audit_log.json", "btc_price_history.json",
    "last_summary.json", "downloaded_paper_trades.json", "adaptive_settings.json",
    "alert_dispatched.lock", "last_alert.lock", "self_learning_history.json",
    "performance_log.json", "engine_err.log", "engine_out.log", "output.log",
]
for fname in stale_state_files:
    if os.path.exists(fname):
        os.remove(fname)
        print(f"removed stale local file: {fname}")

# --- 4. Clean up macOS/Python cruft ---
if os.path.exists(".DS_Store"):
    os.remove(".DS_Store")
    print("removed .DS_Store")

if os.path.exists("__pycache__"):
    shutil.rmtree("__pycache__")
    print("removed __pycache__")

print(f"\nDone. Old files archived to ./{ARCHIVE}/ (not deleted, just moved aside).")
