import os
import json
from datetime import datetime

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
LEDGER_FILE = os.path.join(DATA_DIR, "ledger.json")
BOT_PNL_FILE = os.path.join(DATA_DIR, "bot_realized_pnl.json")

SPORTS_STAKE_PER_TRADE = float(os.getenv("SPORTS_STAKE_PER_TRADE", "5.00"))
BTC_STAKE_PER_TRADE = float(os.getenv("BTC_STAKE_PER_TRADE", "2.00"))

# How much extra account equity beyond (allocated + realized profit) has to
# show up before we treat it as a genuine new deposit, not rounding noise.
DEPOSIT_DETECTION_THRESHOLD = 0.50


def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {
        "total_allocated": 0.0,
        "pending_deposit_amount": None,
        "history": [],  # list of {type, amount, at} for deposits/allocations
    }


def save_ledger(ledger):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


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
    if os.path.exists(BOT_PNL_FILE):
        with open(BOT_PNL_FILE) as f:
            return json.load(f)
    return {"total": 0.0, "history": []}


def save_bot_pnl(data):
    with open(BOT_PNL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def record_bot_trade_result(ticker, pnl, note=""):
    data = load_bot_pnl()
    data["total"] = round(data["total"] + pnl, 4)
    data["history"].append({"ticker": ticker, "pnl": pnl, "note": note, "at": datetime.now().isoformat()})
    save_bot_pnl(data)
    return data["total"]


def get_bot_realized_profit():
    return load_bot_pnl()["total"]


def get_realized_profit_total(client):
    """
    Sums Kalshi's own authoritative realized_pnl_dollars across every
    position ever traded -- this is real profit/loss data straight from
    the exchange, not something we compute ourselves.
    """
    try:
        positions = client.portfolio.get_positions()
        total = 0.0
        for p in positions:
            pnl = getattr(p, "realized_pnl_dollars", None)
            if pnl:
                total += float(pnl)
        return total
    except Exception as e:
        print(f"[ledger] could not fetch realized P&L: {e}")
        return 0.0


def get_open_position_value(client):
    """
    Sums current market exposure of everything still open, using Kalshi's
    own live data -- this is what current open positions are actually
    worth right now, not their original stake.
    """
    try:
        positions = client.portfolio.get_positions()
        total = 0.0
        for p in positions:
            if float(getattr(p, "position_fp", 0) or 0) != 0:
                exposure = getattr(p, "market_exposure_dollars", None)
                if exposure:
                    total += float(exposure)
        return total
    except Exception as e:
        print(f"[ledger] could not fetch open position value: {e}")
        return 0.0


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
    Compares real account equity (cash + open position value) against what
    we'd expect given only allocated budget + realized profit. Any excess
    beyond a small noise threshold means real money appeared that wasn't
    accounted for -- i.e. a new deposit. Flags it and asks for an
    allocation via the dashboard (Discord can't do two-way replies).
    """
    ledger = load_ledger()

    if ledger.get("pending_deposit_amount"):
        return  # already waiting on the user for a prior deposit

    try:
        balance = client.portfolio.get_balance().balance / 100.0
    except Exception as e:
        print(f"[ledger] could not fetch balance: {e}")
        return

    open_value = get_open_position_value(client)
    realized_profit = get_realized_profit_total(client)

    actual_equity = balance + open_value
    expected_equity = ledger["total_allocated"] + realized_profit

    surplus = actual_equity - expected_equity

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
