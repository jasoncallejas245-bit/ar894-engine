import os
from datetime import datetime

from state_io import atomic_write_json, safe_read_json

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
LEDGER_FILE = os.path.join(DATA_DIR, "ledger.json")
BOT_PNL_FILE = os.path.join(DATA_DIR, "bot_realized_pnl.json")

# Every trade -- sports or BTC -- stakes a PERCENTAGE of available budget,
# not a fixed dollar amount, so it automatically scales with account size
# (e.g. 25% of a $15 available budget = $3.75, 25% of $20 = $5), instead of
# staying stuck at one number regardless of what's actually available.
STAKE_PERCENT = float(os.getenv("STAKE_PERCENT", "0.25"))
STAKE_MIN_DOLLARS = float(os.getenv("STAKE_MIN_DOLLARS", "1.00"))

# How much extra account equity beyond (allocated + realized profit) has to
# show up before we treat it as a genuine new deposit, not rounding noise.
DEPOSIT_DETECTION_THRESHOLD = 0.50


def load_ledger():
    return safe_read_json(LEDGER_FILE, {
        "total_allocated": 0.0,
        "pending_deposit_amount": None,
        "history": [],  # list of {type, amount, at} for deposits/allocations
    })


def save_ledger(ledger):
    atomic_write_json(LEDGER_FILE, ledger)


def load_bot_pnl():
    """
    The bot's OWN realized P&L, tracked separately from Kalshi's
    account-wide realized_pnl_dollars figure. Necessary because this
    account also has manual trading on it -- Kalshi has no concept of
    "which trades came from the bot", so we track it ourselves, scoped
    to only positions this bot itself opened and closed/settled.
    Starts at $0 from whenever this tracking was added; it can't
    retroactively reconstruct P&L from before that, but everything the
    bot does from here on is recorded accurately and separately from
    your manual trades.
    """
    return safe_read_json(BOT_PNL_FILE, {"total": 0.0, "history": []})


def save_bot_pnl(data):
    atomic_write_json(BOT_PNL_FILE, data)


def record_bot_trade_result(ticker, pnl, note=""):
    data = load_bot_pnl()
    data["total"] = round(data["total"] + pnl, 4)
    data["peak"] = round(max(data.get("peak", 0.0), data["total"]), 4)
    data["history"].append({"ticker": ticker, "pnl": pnl, "note": note, "at": datetime.now().isoformat()})
    save_bot_pnl(data)
    return data["total"]


def get_bot_realized_profit():
    return load_bot_pnl()["total"]


# How much the bot's own real-money lifetime P&L is allowed to fall from
# its peak (not just from zero -- this also catches giving back a big
# chunk of real profit, not only a net loss from scratch) before real
# trading auto-halts. Paper trading is never affected by this -- it's a
# real-money-only safety net.
MAX_DRAWDOWN_DOLLARS = float(os.getenv("MAX_DRAWDOWN_DOLLARS", "10.00"))


def get_drawdown():
    """Dollars below the bot's own real-money peak realized P&L right now."""
    data = load_bot_pnl()
    return round(data.get("peak", 0.0) - data.get("total", 0.0), 4)


def is_trading_halted():
    return load_bot_pnl().get("halted", False)


def halt_trading(reason):
    """Returns True if this call is what actually flipped the halt (so the
    caller knows to send exactly one alert, not one every cycle)."""
    data = load_bot_pnl()
    if data.get("halted"):
        return False
    data["halted"] = True
    data["halted_reason"] = reason
    data["halted_at"] = datetime.now().isoformat()
    save_bot_pnl(data)
    return True


def resume_trading():
    data = load_bot_pnl()
    data["halted"] = False
    data["halted_reason"] = None
    data["resumed_at"] = datetime.now().isoformat()
    save_bot_pnl(data)


def check_drawdown_circuit_breaker(send_discord_fn, webhook):
    """
    Call this right after any REAL trade settles (win, loss, or early
    profit-take). If the bot's real-money lifetime P&L has fallen
    MAX_DRAWDOWN_DOLLARS or more below its own peak, halts ALL real
    trading (sports + BTC) immediately -- paper trading keeps running
    untouched. Sends exactly one Discord alert when the halt first
    triggers; does not spam on every cycle after that. Re-enabling is a
    manual action (resume_trading / POST /resume_trading) so a losing
    streak can't quietly turn back on by itself.
    """
    dd = get_drawdown()
    if dd >= MAX_DRAWDOWN_DOLLARS and halt_trading(f"drawdown ${dd:.2f} >= limit ${MAX_DRAWDOWN_DOLLARS:.2f}"):
        send_discord_fn(
            webhook,
            f"\U0001F6D1 REAL TRADING AUTO-HALTED, sir. Drawdown from the bot's own peak "
            f"real-money profit hit ${dd:.2f} (limit ${MAX_DRAWDOWN_DOLLARS:.2f}). "
            f"All real-money trading is stopped -- paper trading keeps running normally "
            f"so you can keep evaluating the strategy. Nothing resumes on its own; "
            f"re-enable manually once you've reviewed what happened."
        )


def get_open_position_cost_basis(open_positions_dict, live_position_tickers):
    """
    Sums the ORIGINAL stake of positions we're still holding (per our own
    open_positions.json, cross-checked against Kalshi's live position list
    so stale local entries for already-settled positions don't count).
    This is what's currently 'tied up' out of the allocated budget.
    """
    total = 0.0
    for ticker, pos in open_positions_dict.items():
        if ticker in live_position_tickers:
            total += pos.get("entry_price", 0) * pos.get("count_fp", 0)
    return total


def get_available_budget(client, open_positions_dict):
    """
    The actual, live-computed amount currently free to trade with.

    Deliberately does NOT try to net out realized P&L anymore -- this
    account also has manual trading on it (unrelated markets, much bigger
    stakes than this bot ever uses), and Kalshi's realized-P&L figure is
    account-wide, so it was pulling manual trading results into the bot's
    own budget math. That's not fixable by adjusting the formula; the
    account-wide number is just the wrong input.

    Instead: the bot may use up to what you've authorized (total_allocated),
    capped at whatever cash is ACTUALLY in the account right now (so it can
    never be told to spend money that isn't there, no matter what else has
    happened on the account), minus whatever it currently has committed to
    its own open positions.
    """
    ledger = load_ledger()
    try:
        positions = client.portfolio.get_positions()
        live_tickers = {p.ticker for p in positions if float(getattr(p, "position_fp", 0) or 0) != 0}
    except Exception:
        live_tickers = set()

    committed = get_open_position_cost_basis(open_positions_dict, live_tickers)

    try:
        live_cash = client.portfolio.get_balance().balance / 100.0
    except Exception as e:
        print(f"[ledger] could not fetch live balance, falling back to allocated-only budget: {e}")
        return max(0.0, ledger["total_allocated"] - committed)

    authorized = min(ledger["total_allocated"], live_cash)
    return max(0.0, authorized - committed)


def check_for_new_deposit(client, send_discord_fn, webhook, dashboard_url):
    """
    Flags real new money: live cash balance exceeding what's ever been
    authorized (total_allocated). Deliberately does NOT factor in realized
    P&L or open-position value anymore -- this account also has manual
    trading on it, and Kalshi's realized P&L is account-wide, so trying to
    "expect" a certain equity level based on it kept producing phantom
    deposit flags. Balance dipping below total_allocated (from a loss, bot
    or manual) is not a "missing deposit" -- get_available_budget already
    handles that by capping at live balance. This only fires when there's
    genuinely more cash sitting in the account than you've ever approved.
    """
    ledger = load_ledger()

    if ledger.get("pending_deposit_amount"):
        return  # already waiting on the user for a prior deposit

    try:
        balance = client.portfolio.get_balance().balance / 100.0
    except Exception as e:
        print(f"[ledger] could not fetch balance: {e}")
        return

    surplus = balance - ledger["total_allocated"]

    if surplus > DEPOSIT_DETECTION_THRESHOLD:
        ledger["pending_deposit_amount"] = round(surplus, 2)
        save_ledger(ledger)
        send_discord_fn(
            webhook,
            f"New deposit detected: approximately ${surplus:.2f}, sir.\n"
            f"I won't touch it until you tell me how much I'm allowed to use. "
            f"Set it on the dashboard: {dashboard_url}"
        )


def approve_allocation(amount):
    """Called from the dashboard when the user sets how much of a pending deposit to allocate."""
    ledger = load_ledger()
    pending = ledger.get("pending_deposit_amount") or 0.0
    amount = max(0.0, min(amount, pending))

    ledger["total_allocated"] += amount
    ledger["history"].append({"type": "allocation", "amount": amount, "pending_was": pending})
    ledger["pending_deposit_amount"] = None
    save_ledger(ledger)
    return ledger
