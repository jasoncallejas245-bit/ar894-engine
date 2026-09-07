import os
import shutil
from datetime import date

ARCHIVE = f"_archive_cleanup_{date.today().isoformat()}"
os.makedirs(ARCHIVE, exist_ok=True)

diagnostic_scripts = [
    "audit_trades.py", "check_kalshi_series.py", "check_kalshi_sports.py",
    "check_kalshi_ticker_format.py", "check_liquidity.py", "check_trades.py",
    "diagnose_btc_hang.py", "dump_one_market.py", "edge_scanner.py",
    "final_order_test.py", "list_kalshi_methods.py", "raw_api_check.py",
    "test_pykalshi.py", "test_trade.py", "update_bot.py",
    "cleanup_project.py",  # archive itself too, its job is done
]

for fname in diagnostic_scripts:
    if os.path.exists(fname):
        shutil.move(fname, os.path.join(ARCHIVE, fname))
        print(f"archived: {fname}")

print(f"\nDone. Production files only remain in the main folder now.")
