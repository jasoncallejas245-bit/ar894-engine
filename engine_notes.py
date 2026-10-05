"""
The engine's own notebook (added 2026-10-05, at the user's request): a
running, dated log of what changed and how each strategy version is doing,
written by the engine itself so there's always a record to look back on.

- note_version_start(): on worker start, logs a note the first time a new
  CURRENT_VERSION goes live.
- maybe_daily_note(): once per day, logs a one-line scoreboard per version.
Notes live in engine_notes.json (newest last, capped). Never raises.
"""
import os
from datetime import datetime

from state_io import atomic_write_json, safe_read_json
import strategy_versions as sv

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
NOTES_FILE = os.path.join(DATA_DIR, "engine_notes.json")
MAX_NOTES = 500


def load_notes():
    return safe_read_json(NOTES_FILE, {"notes": [], "last_version": None, "last_daily": None})


def add_note(kind, text, data=None):
    try:
        d = load_notes()
        d["notes"].append({"at": datetime.now().isoformat(timespec="seconds"), "v": sv.CURRENT_VERSION, "kind": kind, "text": text})
        d["notes"] = d["notes"][-MAX_NOTES:]
        if data:
            d.update(data)
        atomic_write_json(NOTES_FILE, d)
    except Exception as e:
        print(f"[engine_notes] add_note error: {e}")


def note_version_start():
    try:
        d = load_notes()
        if d.get("last_version") == sv.CURRENT_VERSION:
            return
        info = sv.get(sv.CURRENT_VERSION) or {}
        add_note("version", f"Now running v{sv.CURRENT_VERSION}: {info.get('name', '')}.",
                 {"last_version": sv.CURRENT_VERSION})
    except Exception as e:
        print(f"[engine_notes] note_version_start error: {e}")


def version_scoreboard(picks):
    """{version: {finished, won, lost, pnl, stake, predicted, implied}} for strong + Pinnacle picks."""
    board = {}
    for p in picks:
        if not (p.get("bet_tier") == "strong" or p.get("sharp_strong")):
            continue
        v = sv.version_of(p)
        b = board.setdefault(v, {"finished": 0, "won": 0, "lost": 0, "open": 0, "pnl": 0.0, "stake": 0.0,
                                 "pred_sum": 0.0, "price_sum": 0.0, "clv_sum": 0.0, "clv_n": 0})
        if p.get("status") in ("won", "lost"):
            b["finished"] += 1
            b[p["status"]] += 1
            b["pnl"] += p.get("hypothetical_pnl") or 0
            b["stake"] += p.get("stake_dollars") or 0
            b["pred_sum"] += p.get("market_probability") or 0
            b["price_sum"] += p.get("entry_price") or 0
            if p.get("closing_price") is not None and p.get("entry_price"):
                b["clv_sum"] += p["closing_price"] - p["entry_price"]
                b["clv_n"] += 1
        elif p.get("status") == "pending":
            b["open"] += 1
    for b in board.values():
        n = b["finished"]
        b["win_rate"] = b["won"] / n if n else None
        b["predicted"] = b["pred_sum"] / n if n else None
        b["needed"] = b["price_sum"] / n if n else None
        b["roi"] = b["pnl"] / b["stake"] * 100 if b["stake"] else None
        b["clv_cents"] = b["clv_sum"] / b["clv_n"] * 100 if b["clv_n"] else None
    return board


def maybe_daily_note(picks):
    try:
        today = datetime.now().date().isoformat()
        d = load_notes()
        if d.get("last_daily") == today:
            return
        board = version_scoreboard(picks)
        parts = []
        for v in sorted(board):
            b = board[v]
            wr = f"{b['win_rate']*100:.0f}% won vs {b['needed']*100:.0f}% needed" if b["finished"] else "nothing finished yet"
            parts.append(f"v{v}: {b['won']}-{b['lost']}, {b['pnl']:+.2f} ({wr}), {b['open']} open")
        add_note("daily", "Daily scoreboard. " + ("; ".join(parts) if parts else "No picks yet."), {"last_daily": today})
    except Exception as e:
        print(f"[engine_notes] maybe_daily_note error: {e}")
