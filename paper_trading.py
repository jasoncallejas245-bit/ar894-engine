import os
import statistics
import requests
from datetime import datetime, timezone

from state_io import atomic_write_json, safe_read_json
import context_data

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
PAPER_TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.json")
BTC_PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "btc_price_history.json")
PAPER_BANKROLL_FILE = os.path.join(DATA_DIR, "paper_bankroll.json")

# Every paper trade now sizes itself like a real $5 bet, instead of the old
# "1 contract at entry price" math -- so the logged hypothetical P&L reflects
# what would actually happen if this pick had been placed for real at the
# stake size this bot actually uses, and the numbers are comparable across
# BTC and moneyline.
PAPER_STAKE_DOLLARS = float(os.getenv("PAPER_STAKE_DOLLARS", "5.0"))

# Starting notional bankroll per category, purely for tracking "are we up or
# down against a hypothetical budget" over time -- has no bearing on real
# money, just makes the paper data readable as a running balance.
PAPER_STARTING_BANKROLL = float(os.getenv("PAPER_STARTING_BANKROLL", "100.0"))


def load_paper_bankroll():
    return safe_read_json(PAPER_BANKROLL_FILE, {
        "moneyline": {"balance": PAPER_STARTING_BANKROLL, "history": []},
        "btc": {"balance": PAPER_STARTING_BANKROLL, "history": []},
    })


def save_paper_bankroll(data):
    atomic_write_json(PAPER_BANKROLL_FILE, data)


def record_paper_bankroll_change(category, pnl, ticker_or_id, note=""):
    """
    Applies one resolved paper trade's P&L to that category's running
    notional bankroll and logs whether it's currently down against its
    starting budget. Returns (new_balance, is_down, down_by).
    """
    bankroll = load_paper_bankroll()
    cat = bankroll.setdefault(category, {"balance": PAPER_STARTING_BANKROLL, "history": []})
    cat["balance"] = round(cat["balance"] + pnl, 4)
    is_down = cat["balance"] < PAPER_STARTING_BANKROLL
    down_by = round(PAPER_STARTING_BANKROLL - cat["balance"], 4) if is_down else 0.0
    cat["history"].append({
        "id": ticker_or_id, "pnl": pnl, "balance_after": cat["balance"],
        "is_down": is_down, "down_by": down_by, "note": note,
        "at": datetime.now().isoformat(),
    })
    save_paper_bankroll(bankroll)
    return cat["balance"], is_down, down_by


def load_paper_trades():
    return safe_read_json(PAPER_TRADES_FILE, {"moneyline": [], "btc": []})


def save_paper_trades(data):
    atomic_write_json(PAPER_TRADES_FILE, data)


# ---------------------------------------------------------------------------
# Shared significance helper -- so "the numbers moved the wrong way" doesn't
# get treated as "we learned something" on a handful of noisy outcomes.
# ---------------------------------------------------------------------------
def _mean_is_significantly_negative(values, z=2.0):
    """
    True only if the sample mean is negative by more than `z` standard
    errors -- i.e. even after giving the sample the benefit of the doubt
    for noise, it still looks like a real loss. z=2.0 is roughly a 97.5%
    one-sided confidence bar (not just "further from zero than 1 SE",
    which turned out to false-positive on pure-noise test data -- 3
    candidate windows being compared at once makes an unlucky one easy to
    hit by chance, so this needs a real margin, not a token one). This
    still isn't a peer-reviewed stats pipeline, but it should not fire on
    "the raw total happened to be negative today."
    """
    if len(values) < 2:
        return False
    mean = statistics.mean(values)
    stderr = statistics.stdev(values) / (len(values) ** 0.5)
    return (mean + z * stderr) < 0


# ---------------------------------------------------------------------------
# Moneyline paper trading
# ---------------------------------------------------------------------------
def purge_stale_moneyline_picks(client, near_term_hours=None):
    """
    One-time cleanup: removes PENDING moneyline paper picks whose game is
    no longer near-term (or whose market can't even be found anymore).
    These were picked before the near-term filter existed, so they were
    piling up for games days/weeks out that were never going to resolve
    soon. Won/lost picks are always kept -- this only clears out picks
    that were never going to give us a useful signal anytime soon.
    Returns (kept_count, removed_count).
    """
    if near_term_hours is None:
        near_term_hours = float(os.getenv("NEAR_TERM_HOURS", "36")) * 2  # a bit lenient

    from datetime import timezone as _tz
    paper_data = load_paper_trades()
    kept, removed = [], 0

    for pick in paper_data["moneyline"]:
        if pick["status"] != "pending":
            kept.append(pick)
            continue

        ticker = pick.get("kalshi_ticker")
        if not ticker:
            removed += 1
            continue

        try:
            market = client.get_market(ticker)
        except Exception:
            removed += 1
            continue

        close_time = getattr(market, "close_time", None)
        if not close_time:
            removed += 1
            continue

        try:
            close_dt = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
        except Exception:
            removed += 1
            continue

        hours_until = (close_dt - datetime.now(_tz.utc)).total_seconds() / 3600
        if 0 <= hours_until <= near_term_hours:
            kept.append(pick)
        else:
            removed += 1

    paper_data["moneyline"] = kept
    save_paper_trades(paper_data)
    return len(kept), removed


def record_live_moneyline_pick(league, event_id, away_team, home_team, picked_team, kalshi_ticker, entry_price, opening_price):
    """
    Records a single live/in-game pick from live_trading.py, reusing the
    exact same moneyline paper-trading store, bankroll, and resolution
    logic (resolve_moneyline_paper_trades) as pregame picks -- no separate
    tracking needed. Tagged source="live" so it's distinguishable from
    pregame picks in /moneyline_picks and can be filtered/removed on its
    own if the live strategy doesn't pan out, without touching pregame
    data at all.

    Shares the SAME already_picked (event_id) dedup as pregame picks --
    a game already paper-picked pregame is never ALSO picked live, and
    vice versa, so one game never gets double exposure across the two
    strategies. Returns True if a pick was recorded, False if this event
    was already picked (by either strategy) or the pick failed to save.
    Never raises.
    """
    try:
        paper_data = load_paper_trades()
        already_picked = {p["event_id"] for p in paper_data["moneyline"]}
        if event_id in already_picked:
            return False

        pick = {
            "league": league.upper(),
            "event_id": event_id,
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": picked_team,
            "side": "YES",
            "market_probability": None,  # no fair-probability model for live -- this trades price action, not a mispricing vs. a known fair value
            "edge_pct": None,
            "kalshi_ticker": kalshi_ticker,
            "entry_price": entry_price,
            "opening_price_when_tracked": opening_price,
            "picked_at": datetime.now().isoformat(),
            "status": "pending",
            "is_too_close": False,
            "context_note": None,
            "source": "live",
        }
        paper_data["moneyline"].append(pick)
        save_paper_trades(paper_data)
        return True
    except Exception as e:
        print(f"[paper_trading] record_live_moneyline_pick failed: {e}")
        return False


import math

def _kalshi_taker_fee_dollars_local(price_dollars, contracts=1.0):
    """
    Kalshi's real taker-order fee -- see worker.kalshi_taker_fee_dollars
    for the full explanation of where this formula comes from and why it
    matters. Kept as a local duplicate (not imported) to avoid a circular
    import, same pattern as the other _local helpers in this file.
    fee = round_up(0.07 * C * P * (1-P))
    """
    raw = 0.07 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(raw * 10000) / 10000.0


MIN_NET_EDGE_AFTER_FEE_PCT = float(os.getenv("MIN_NET_EDGE_AFTER_FEE_PCT", "1.0"))


def _clears_fee_adjusted_edge_local(edge_pct, price_dollars, min_edge_pct):
    """Same logic as worker.clears_fee_adjusted_edge -- local copy to avoid
    a circular import. True if edge_pct clears the raw threshold AND still
    leaves MIN_NET_EDGE_AFTER_FEE_PCT of edge after Kalshi's real fee."""
    if edge_pct < min_edge_pct:
        return False
    fee_pct = _kalshi_taker_fee_dollars_local(price_dollars) * 100
    return (edge_pct - fee_pct) >= MIN_NET_EDGE_AFTER_FEE_PCT


def _normalize_team_name_local(name):
    """
    Same normalization worker.py uses for Kalshi matching -- kept local
    here (not imported) to avoid a circular import between worker.py and
    paper_trading.py. Exact-equality safe, handles 'St.' vs 'State'.
    """
    n = (name or "").upper().strip()
    n = n.replace(" ST.", " STATE").replace(" ST ", " STATE ")
    if n.endswith(" ST"):
        n = n[:-3] + " STATE"
    n = n.replace(".", "").replace("  ", " ")
    return n.strip()


def _selection_side(selection, away_team, home_team):
    """
    Figures out whether a sportsbook row's raw 'selection' string refers to
    the away or home team, WITHOUT assuming every book spells team names
    the same way -- e.g. DraftKings says 'CLE Guardians' while FanDuel
    says 'Cleveland Guardians' for the identical team in the identical
    game. Matches on the mascot (the last word of the normalized name),
    which stays the same across abbreviated and full spellings, and falls
    back to exact full-name equality. Returns 'away', 'home', or None if
    neither can be determined (row is skipped rather than guessed at).
    """
    def mascot(name):
        parts = _normalize_team_name_local(name).split()
        return parts[-1] if parts else ""

    sel_mascot = mascot(selection)
    away_mascot, home_mascot = mascot(away_team), mascot(home_team)

    if sel_mascot and sel_mascot == away_mascot and sel_mascot != home_mascot:
        return "away"
    if sel_mascot and sel_mascot == home_mascot and sel_mascot != away_mascot:
        return "home"

    sel_full = _normalize_team_name_local(selection)
    if sel_full and sel_full == _normalize_team_name_local(away_team):
        return "away"
    if sel_full and sel_full == _normalize_team_name_local(home_team):
        return "home"
    return None


def _longest_names_row(rows):
    """
    Picks whichever row (from any iterable of sportsbook rows) has the
    longest combined away_team + home_team text -- prefers the
    unabbreviated spelling (FanDuel's "Boston Red Sox") over an
    abbreviated one (DraftKings' "BOS Red Sox") without a hardcoded
    per-team abbreviation table. Matters because away_team/home_team
    feed straight into Kalshi matching.
    """
    return max(rows, key=lambda r: len(r.get("away_team") or "") + len(r.get("home_team") or ""))


def make_moneyline_paper_picks(league, sharpapi_rows, kalshi_events, safe_match_fn, send_discord_fn, webhook, min_edge_pct=2.0, favorite_min_prob=None):
    """
    Mirrors the REAL trading edge-detection logic exactly (checks both YES
    and NO for a genuine mispricing edge, skips the game entirely if
    neither side clears the bar, and only backs the side actually favored
    to win -- same FAVORITE_MIN_PROB bar real trading uses) -- so paper
    trading actually validates the same method used for real money, just
    extended to more sports and with no cap on how many picks it can make.
    """
    from collections import defaultdict as dd

    if favorite_min_prob is None:
        favorite_min_prob = get_effective_favorite_min_prob()  # picks up whatever the learning step has raised it to

    grouped = dd(dict)
    for row in sharpapi_rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        side = _selection_side(row.get("selection"), row.get("away_team"), row.get("home_team"))
        if side is None:
            continue  # can't tell which team this selection refers to -- skip rather than guess
        key = (row.get("event_id"), side)
        grouped[key][row.get("sportsbook")] = row

    by_event = dd(set)
    for (event_id, side) in grouped.keys():
        by_event[event_id].add(side)

    paper_data = load_paper_trades()
    already_picked = {p["event_id"] for p in paper_data["moneyline"]}
    new_picks = []

    # Funnel counters -- added to see exactly WHERE games are getting
    # filtered out (no Kalshi listing? wrong day? no real edge?) instead
    # of just seeing "0 picks" with no way to tell why. Printed once per
    # call, cheap, diagnostic only.
    funnel = {
        "distinct_events": len(by_event), "already_picked": 0, "no_two_sided_odds": 0,
        "no_common_book": 0, "no_prob_data": 0, "not_today_or_started": 0,
        "no_kalshi_match": 0, "no_qualifying_edge": 0, "picked": 0,
    }

    for event_id, selections in by_event.items():
        if event_id in already_picked:
            funnel["already_picked"] += 1
            continue
        if len(selections) != 2:
            funnel["no_two_sided_odds"] += 1
            continue
        sel_a, sel_b = list(selections)  # canonical "away"/"home" labels now, not raw selection text
        rows_a, rows_b = grouped[(event_id, sel_a)], grouped[(event_id, sel_b)]
        common_books = set(rows_a.keys()) & set(rows_b.keys())
        if not common_books:
            funnel["no_common_book"] += 1
            continue

        probs_a, probs_b = [], []
        for book in common_books:
            pa, pb = rows_a[book].get("odds_probability"), rows_b[book].get("odds_probability")
            if pa is None or pb is None:
                continue
            total = pa + pb
            probs_a.append(pa / total)
            probs_b.append(pb / total)

        if not probs_a:
            funnel["no_prob_data"] += 1
            continue

        fair_a, fair_b = sum(probs_a) / len(probs_a), sum(probs_b) / len(probs_b)
        # Pick ONE row (from either side, whichever book has the longest
        # combined away+home text) so away_team/home_team come from the
        # same book consistently, rather than potentially mixing an
        # abbreviated field from one book with a full-name field from
        # another.
        best_row = _longest_names_row(list(rows_a.values()) + list(rows_b.values()))
        away_team, home_team = best_row.get("away_team"), best_row.get("home_team")
        team_by_side = {"away": away_team, "home": home_team}
        fair_by_side = {"away": fair_a if sel_a == "away" else fair_b, "home": fair_b if sel_a == "away" else fair_a}

        # Same-day only -- mirrors real trading: the event must start later today
        # (UTC), not just within some rolling hour window that could roll into
        # tomorrow. Otherwise picks pile up for games that can't resolve soon.
        start_str = best_row.get("event_start_time")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception:
            continue
        now = datetime.now(timezone.utc)
        hours_until = (start_dt - now).total_seconds() / 3600
        if not (hours_until >= 0 and start_dt.date() == now.date()):
            funnel["not_today_or_started"] += 1
            continue

        match_map = safe_match_fn(kalshi_events, away_team, home_team, individual=(league in {'ufc', 'atp'}))  # matches worker.py's INDIVIDUAL_ATHLETE_LEAGUES
        if not match_map:
            funnel["no_kalshi_match"] += 1
            continue

        # match_map is keyed by the canonical away_team/home_team strings
        # (see safe_match_event in worker.py), not by side label -- so look
        # up picks by team name, and track fair probability by side label.
        fair_probs = {team_by_side["away"]: fair_by_side["away"], team_by_side["home"]: fair_by_side["home"]}

        # Check BOTH selections for a genuine YES-side edge, same as real
        # trading. NO-side edges surface naturally too, since if team A's
        # fair prob is well below the market's YES price, that's really a
        # NO-side edge on team A (equivalent to a YES edge on team B).
        best_pick = None
        for selection in (team_by_side["away"], team_by_side["home"]):
            match = match_map.get(selection)
            if not match:
                continue
            yes_ask = getattr(match, "yes_ask_dollars", None)
            if not yes_ask:
                continue
            yes_price = float(yes_ask)
            fair_prob = fair_probs[selection]
            edge_pct = (fair_prob - yes_price) * 100

            if _clears_fee_adjusted_edge_local(edge_pct, yes_price, min_edge_pct) and fair_prob >= favorite_min_prob:
                candidate = {
                    "picked_team": selection, "side": "YES",
                    "market_probability": fair_prob, "entry_price": yes_price,
                    "edge_pct": edge_pct, "kalshi_ticker": match.ticker,
                }
                if best_pick is None or edge_pct > best_pick["edge_pct"]:
                    best_pick = candidate

        if best_pick is None:
            funnel["no_qualifying_edge"] += 1
            continue  # no genuine edge on either side -- skip, don't force a pick

        funnel["picked"] += 1

        # "Too close to call" = it cleared the favorite bar but only just --
        # same band the learning step (maybe_adjust_moneyline_favorite_threshold)
        # already watches for a losing pattern. For picks in that band, pull a
        # free injury/weather context note (ESPN + NWS, see context_data.py)
        # so there's something to look at besides the raw odds when reviewing
        # why a "too close" pick did or didn't work out. This is informational
        # only right now -- it does NOT change the pick or the probability.
        is_too_close = favorite_min_prob <= best_pick["market_probability"] < (favorite_min_prob + CLOSE_GAME_BAND)
        context_note = None
        if is_too_close:
            try:
                context_note = context_data.get_context_note(league, away_team, home_team)
            except Exception as e:
                print(f"[context_data] lookup failed for {away_team} @ {home_team}: {e}")

        pick = {
            "league": league.upper(),
            "event_id": event_id,
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": best_pick["picked_team"],
            "side": best_pick["side"],
            "market_probability": best_pick["market_probability"],
            "edge_pct": best_pick["edge_pct"],
            "kalshi_ticker": best_pick["kalshi_ticker"],
            "entry_price": best_pick["entry_price"],
            "picked_at": datetime.now().isoformat(),
            "status": "pending",
            "is_too_close": is_too_close,
            "context_note": context_note,
            "source": "pregame",
        }
        paper_data["moneyline"].append(pick)
        new_picks.append(pick)

    print(f"[{league}] moneyline funnel: {funnel}")

    if new_picks:
        save_paper_trades(paper_data)
        lines = [f"[PAPER TRADES - {league.upper()}] {len(new_picks)} genuine-edge picks this cycle:\n"]
        for p in new_picks:
            lines.append(
                f"- {p['picked_team']} [{p['side']}] ({p['away_team']} @ {p['home_team']}) "
                f"| fair {p['market_probability']*100:.1f}% | Kalshi ${p['entry_price']:.2f} | edge +{p['edge_pct']:.1f}%"
                + (" [TOO CLOSE]" if p["is_too_close"] else "")
            )
            if p.get("context_note"):
                lines.append(f"  Context: {p['context_note'].replace(chr(10), ' | ')}")
        lines.append("\n(No real money -- tracking the same edge method used for real trading)")
        send_discord_fn(webhook, "\n".join(lines))

    return new_picks


def resolve_moneyline_paper_trades(client, send_discord_fn=None, webhook=None):
    """
    Checks each pending moneyline pick's Kalshi ticker for a settlement
    result, marks it won/lost, and computes what the actual hypothetical
    P&L would have been using the real entry price captured at pick time.
    """
    paper_data = load_paper_trades()
    changed = False

    for pick in paper_data["moneyline"]:
        if pick["status"] != "pending" or not pick.get("kalshi_ticker"):
            continue

        try:
            market = client.get_market(pick["kalshi_ticker"])
        except Exception:
            continue

        result = getattr(market, "result", None)
        if result not in ("yes", "no"):
            continue  # not settled yet

        won = (result == "yes")
        pick["status"] = "won" if won else "lost"
        pick["resolved_at"] = datetime.now().isoformat()

        if pick.get("entry_price"):
            # $5-stake simulation: same sizing math real trading uses
            # (stake_dollars / price, minimum 1 contract), not the old
            # "1 contract at entry_price" model, so the hypothetical P&L
            # reflects what a real $5 bet on this pick would have made.
            contracts = max(1.0, PAPER_STAKE_DOLLARS / pick["entry_price"])
            pick["stake_dollars"] = round(pick["entry_price"] * contracts, 4)
            pick["contracts"] = contracts
            # Kalshi's real taker fee, paid on entry regardless of win/loss --
            # never subtracted before (2026-09-08), which meant this
            # "hypothetical P&L" was overstating true profitability on
            # every single resolved trade. See _kalshi_taker_fee_dollars_local.
            entry_fee = _kalshi_taker_fee_dollars_local(pick["entry_price"], contracts)
            pick["hypothetical_pnl"] = round(((1.0 - pick["entry_price"]) * contracts if won else -pick["entry_price"] * contracts) - entry_fee, 4)
        else:
            pick["hypothetical_pnl"] = None

        changed = True

        balance, is_down, down_by = (None, None, None)
        if pick["hypothetical_pnl"] is not None:
            balance, is_down, down_by = record_paper_bankroll_change(
                "moneyline", pick["hypothetical_pnl"], pick.get("kalshi_ticker"),
                note=f"{pick['league']} {pick['picked_team']} {pick['status']}",
            )

        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}" if pick["hypothetical_pnl"] is not None else "N/A (no entry price captured)"
            bankroll_str = ""
            if balance is not None:
                bankroll_str = f" | paper bankroll: ${balance:.2f}" + (f" (down ${down_by:.2f})" if is_down else "")
            send_discord_fn(
                webhook,
                f"[RESOLVED - {pick['league']}] {pick['picked_team']}: {pick['status'].upper()} "
                f"(hypothetical P&L: {pnl_str}{bankroll_str})",
            )

    if changed:
        save_paper_trades(paper_data)
    return changed


# ---------------------------------------------------------------------------
# BTC 15-min paper trading
# ---------------------------------------------------------------------------
def get_btc_spot_price():
    try:
        resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        print(f"[btc] price fetch failed: {e}")
        return None


def load_btc_price_history():
    return safe_read_json(BTC_PRICE_HISTORY_FILE, [])


def save_btc_price_history(history):
    history = history[-20:]
    atomic_write_json(BTC_PRICE_HISTORY_FILE, history, indent=None)


# Windows this actually tries when "learning" a better momentum lookback.
# Kept small and simple on purpose -- this is meant to be a real, checkable
# improvement over a hardcoded number, not a hyperparameter search.
BTC_MOMENTUM_WINDOW_CANDIDATES = (2, 3, 5)
BTC_MOMENTUM_WINDOW_DEFAULT = 3

# Confirmed live: over 75 resolved bets the strategy hit a 61.3% win rate
# and was STILL down $32.84 overall -- winning most of its bets while
# losing money means the PRICE it pays matters just as much as which
# direction it guesses, and the original version never checked that at
# all (it bought whichever side momentum favored at whatever price the
# market offered). This is the fix: only take a bet when the price
# leaves real room for profit given how often this strategy is actually
# right, not just whenever momentum points somewhere.
BTC_MIN_EDGE_PCT = float(os.getenv("BTC_MIN_EDGE_PCT", "3.0"))


def get_effective_btc_momentum_window():
    """The currently-learned BTC momentum lookback (see
    maybe_adjust_btc_momentum_window for how/when this changes)."""
    return load_adaptive_settings().get("btc_momentum_window", BTC_MOMENTUM_WINDOW_DEFAULT)


def get_btc_fair_prob_estimate():
    """
    Best available stand-in for "how likely is this strategy actually
    right" -- used to gate BTC trades on price, not just direction. This
    doesn't have a real calibrated probability model (that would need
    predicting the SIZE of the move, not just its direction), so it uses
    the live, resolved win rate of the CURRENTLY active window as the
    estimate. That's not perfect -- it doesn't split UP vs DOWN, which
    could have different true odds -- but it's real data, honestly
    describes what this strategy has actually done, and directly targets
    the exact failure mode confirmed above.

    Returns None before there's enough resolved history to trust it (the
    same MIN_SAMPLE_FOR_ADJUSTMENT bar the window-learning itself uses)
    -- during that bootstrap window BTC still trades on direction alone,
    same as before this fix, so early data collection isn't blocked by a
    number that isn't trustworthy yet.
    """
    settings = load_adaptive_settings()
    if settings.get("btc_sample_size", 0) < MIN_SAMPLE_FOR_ADJUSTMENT:
        return None
    return settings.get("btc_win_rate")


def make_btc_paper_pick(client, MarketStatus, send_discord_fn, webhook):
    price = get_btc_spot_price()
    if price is None:
        return None

    history = load_btc_price_history()
    history.append({"price": price, "at": datetime.now().isoformat()})
    save_btc_price_history(history)

    window = get_effective_btc_momentum_window()
    if len(history) < window:
        return None

    momentum = history[-1]["price"] - history[-window]["price"]
    direction = "up" if momentum > 0 else "down"

    try:
        markets = client.get_markets(series_ticker="KXBTC15M", status=MarketStatus.OPEN, limit=5)
    except Exception as e:
        print(f"[btc] market fetch failed: {e}")
        return None

    if not markets:
        return None

    market = sorted(markets, key=lambda m: getattr(m, "close_time", None) or "9999")[0]

    paper_data = load_paper_trades()
    already_picked = {p["ticker"] for p in paper_data["btc"]}
    if market.ticker in already_picked:
        return None

    entry_price = None
    yes_ask = getattr(market, "yes_ask_dollars", None)
    no_ask = getattr(market, "no_ask_dollars", None)
    if direction == "up" and yes_ask:
        entry_price = float(yes_ask)
    elif direction == "down" and no_ask:
        entry_price = float(no_ask)

    # Price-discipline gate (see get_btc_fair_prob_estimate) -- skip a
    # pick where the price doesn't leave enough room for profit given
    # this strategy's real track record, instead of taking every signal
    # regardless of what it costs.
    fair_prob_estimate = get_btc_fair_prob_estimate()
    if fair_prob_estimate is not None and entry_price is not None:
        edge_pct = (fair_prob_estimate - entry_price) * 100
        if not _clears_fee_adjusted_edge_local(edge_pct, entry_price, BTC_MIN_EDGE_PCT):
            return None

    # Snapshot enough recent prices to retroactively test EVERY candidate
    # window later (max window is 5, so keep 6: one more than needed, as a
    # margin) -- this is what makes maybe_adjust_btc_momentum_window able to
    # actually compare windows against real outcomes instead of just logging
    # a number nothing reads back.
    max_window = max(BTC_MOMENTUM_WINDOW_CANDIDATES)
    price_snapshot = [h["price"] for h in history[-(max_window + 1):]]

    pick = {
        "ticker": market.ticker,
        "title": market.title,
        "predicted_direction": direction,
        "btc_price_at_pick": price,
        "momentum_signal": momentum,
        "momentum_window_used": window,
        "price_snapshot": price_snapshot,
        "entry_price": entry_price,
        "picked_at": datetime.now().isoformat(),
        "status": "pending",
        # Contract price over the life of the trade (NOT the BTC spot price
        # above -- this is the Kalshi contract's own bid, i.e. what this
        # pick could be sold for right now) -- filled in by
        # track_btc_contract_prices() on every fast cycle. Recorded so a
        # real early-exit threshold (cash out at some % gain instead of
        # holding to full 15-min resolution) can eventually be picked from
        # actual price paths instead of guessed at.
        "contract_price_history": [],
    }
    paper_data["btc"].append(pick)
    save_paper_trades(paper_data)

    # Notifying on every paper pick got noisy since it fires far more often
    # than real trades and never risks money -- default OFF, opt back in
    # with BTC_PAPER_NOTIFY=true if you want the pings again.
    if os.getenv("BTC_PAPER_NOTIFY", "false").lower() == "true":
        price_note = f"${entry_price:.2f}" if entry_price else "price unavailable"
        msg = (
            f"[PAPER TRADE - BTC 15min] Predicting: {direction.upper()}\n"
            f"Market: {market.title}\n"
            f"BTC price now: ${price:,.2f} (momentum: {momentum:+.2f}, window: {window})\n"
            f"Entry price: {price_note}\n"
            f"(No real money -- experimental signal, tracking for accuracy and P&L)"
        )
        send_discord_fn(webhook, msg)
    return pick


# Bound how much contract-price history one pending BTC pick keeps -- a
# 15-minute market checked on a fast cycle won't need many more than this
# before it resolves, so this can't grow unbounded.
BTC_CONTRACT_HISTORY_MAX = 30


def track_btc_contract_prices(client):
    """
    Runs on the fast cycle for every still-PENDING BTC paper pick: records
    what that contract could be sold for RIGHT NOW (the bid on whichever
    side this pick actually holds), building up a real price path for
    each trade's lifetime. Entirely separate from the BTC SPOT price
    history used for the momentum signal -- this is the Kalshi contract's
    own price, which is what an early-exit decision would actually act on.

    This is data collection only -- it does NOT close any paper position
    early. Once enough trades have a real price path recorded, that data
    can be used to pick an evidence-based early-exit threshold instead of
    guessing at a percentage. Never raises.
    """
    try:
        paper_data = load_paper_trades()
        pending = [p for p in paper_data["btc"] if p["status"] == "pending"]
        if not pending:
            return

        changed = False
        now_iso = datetime.now().isoformat()
        for pick in pending:
            try:
                market = client.get_market(pick["ticker"])
            except Exception:
                continue

            bid_field = "yes_bid_dollars" if pick["predicted_direction"] == "up" else "no_bid_dollars"
            bid = getattr(market, bid_field, None)
            if not bid:
                continue

            pick.setdefault("contract_price_history", [])
            pick["contract_price_history"].append({"at": now_iso, "price": float(bid)})
            pick["contract_price_history"] = pick["contract_price_history"][-BTC_CONTRACT_HISTORY_MAX:]
            changed = True

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] track_btc_contract_prices error: {e}")


# Backed by real tracked-price data (2026-09-09): across 38 BTC paper trades
# with a recorded contract price path, holding every position to full 15-min
# settlement netted -$9.40 total. Simulating "cash out the moment the
# contract's own bid first reaches this price" on those same 38 trades
# netted +$11 to +$20 instead -- a handful of positions that spiked to 90%+
# implied probability and then fully reversed to a loss account for nearly
# all of the difference. Small sample, so this is a live experiment, not a
# proven edge -- paper-only (real BTC trading is off) so it builds forward
# evidence before anything is ever risked for real. Tune or disable via env.
BTC_PAPER_EARLY_EXIT_ENABLED = os.getenv("BTC_PAPER_EARLY_EXIT_ENABLED", "true").lower() == "true"
BTC_PAPER_EARLY_EXIT_PROB = float(os.getenv("BTC_PAPER_EARLY_EXIT_PROB", "0.80"))


def check_and_close_btc_paper_early(send_discord_fn=None, webhook=None):
    """
    Runs on the fast cycle, right after track_btc_contract_prices records
    each pending BTC pick's latest contract bid. If that bid has reached
    BTC_PAPER_EARLY_EXIT_PROB, closes the paper position out AT THAT PRICE
    instead of waiting for full settlement -- exactly what selling the
    contract for real would do. Both the entry fee and this exit's own
    taker fee are charged, same as a real round-trip would cost. Paper-only;
    never touches a real position. Never raises.
    """
    if not BTC_PAPER_EARLY_EXIT_ENABLED:
        return
    try:
        paper_data = load_paper_trades()
        changed = False
        for pick in paper_data["btc"]:
            if pick["status"] != "pending":
                continue
            history = pick.get("contract_price_history") or []
            if not history or not pick.get("entry_price"):
                continue
            latest = history[-1]["price"]
            if latest < BTC_PAPER_EARLY_EXIT_PROB:
                continue

            entry_price = pick["entry_price"]
            contracts = max(1.0, PAPER_STAKE_DOLLARS / entry_price)
            entry_fee = _kalshi_taker_fee_dollars_local(entry_price, contracts)
            exit_fee = _kalshi_taker_fee_dollars_local(latest, contracts)
            pnl = round(contracts * (latest - entry_price) - entry_fee - exit_fee, 4)

            pick["status"] = "won" if pnl > 0 else "lost"
            pick["exit_reason"] = "early_profit_target"
            pick["exit_price"] = latest
            pick["stake_dollars"] = round(entry_price * contracts, 4)
            pick["contracts"] = contracts
            pick["hypothetical_pnl"] = pnl
            pick["resolved_at"] = datetime.now().isoformat()
            changed = True

            balance, is_down, down_by = record_paper_bankroll_change(
                "btc", pnl, pick.get("ticker"),
                note=f"BTC early-exit at ${latest:.2f} ({pick['predicted_direction']})",
            )
            if send_discord_fn and webhook:
                send_discord_fn(
                    webhook,
                    f"[PAPER EARLY EXIT] {pick['ticker']} cashed out at ${latest:.2f} "
                    f"(entry ${entry_price:.2f}) -- ${pnl:+.2f}. BTC paper bankroll: ${balance:.2f}",
                )

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] check_and_close_btc_paper_early error: {e}")


def resolve_btc_paper_trades(client, send_discord_fn=None, webhook=None):
    paper_data = load_paper_trades()
    changed = False

    for pick in paper_data["btc"]:
        if pick["status"] != "pending":
            continue
        try:
            market = client.get_market(pick["ticker"])
        except Exception:
            continue

        result = getattr(market, "result", None)
        if result not in ("yes", "no"):
            continue

        actual_direction = "up" if result == "yes" else "down"
        won = (actual_direction == pick["predicted_direction"])
        pick["status"] = "won" if won else "lost"
        pick["actual_direction"] = actual_direction  # needed to retroactively score other windows
        pick["resolved_at"] = datetime.now().isoformat()

        if pick.get("entry_price"):
            contracts = max(1.0, PAPER_STAKE_DOLLARS / pick["entry_price"])
            pick["stake_dollars"] = round(pick["entry_price"] * contracts, 4)
            pick["contracts"] = contracts
            # Kalshi's real taker fee, paid on entry regardless of win/loss --
            # never subtracted before (2026-09-08), which meant this
            # "hypothetical P&L" was overstating true profitability on
            # every single resolved trade. See _kalshi_taker_fee_dollars_local.
            entry_fee = _kalshi_taker_fee_dollars_local(pick["entry_price"], contracts)
            pick["hypothetical_pnl"] = round(((1.0 - pick["entry_price"]) * contracts if won else -pick["entry_price"] * contracts) - entry_fee, 4)
        else:
            pick["hypothetical_pnl"] = None

        changed = True

        balance, is_down, down_by = (None, None, None)
        if pick["hypothetical_pnl"] is not None:
            balance, is_down, down_by = record_paper_bankroll_change(
                "btc", pick["hypothetical_pnl"], pick.get("ticker"),
                note=f"BTC {pick['predicted_direction']} {pick['status']}",
            )

        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}" if pick["hypothetical_pnl"] is not None else "N/A"
            bankroll_str = ""
            if balance is not None:
                bankroll_str = f" | paper bankroll: ${balance:.2f}" + (f" (down ${down_by:.2f})" if is_down else "")
            send_discord_fn(webhook, f"[RESOLVED - BTC] {pick['title']}: {pick['status'].upper()} (hypothetical P&L: {pnl_str}{bankroll_str})")

    if changed:
        save_paper_trades(paper_data)
    return changed


def get_paper_trade_summary():
    paper_data = load_paper_trades()
    summary = {}
    for category in ["moneyline", "btc"]:
        trades = paper_data[category]
        resolved = [t for t in trades if t["status"] in ("won", "lost")]
        wins = [t for t in resolved if t["status"] == "won"]
        pnls = [t["hypothetical_pnl"] for t in resolved if t.get("hypothetical_pnl") is not None]

        summary[category] = {
            "total_picks": len(trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": (len(wins) / len(resolved) * 100) if resolved else None,
            "total_hypothetical_pnl": sum(pnls) if pnls else None,
            "pnl_sample_size": len(pnls),
        }
    return summary


# ---------------------------------------------------------------------------
# Gated self-adjustment: only kicks in once there's a real sample size AND
# the effect clears the significance check above. Below the threshold, or
# below significance, this does nothing -- adjusting on a tiny or noisy
# sample would just be tuning to noise, not learning anything real.
# ---------------------------------------------------------------------------
MIN_SAMPLE_FOR_ADJUSTMENT = 30

ADAPTIVE_SETTINGS_FILE = os.path.join(DATA_DIR, "adaptive_settings.json")
MONEYLINE_FAVORITE_MIN_PROB_DEFAULT = float(os.getenv("FAVORITE_MIN_PROB", "0.55"))
# How far above the current favorite bar counts as "close enough that we
# shouldn't yet trust it" -- e.g. a 0.55 bar with a 0.05 band means picks
# with fair prob in [0.55, 0.60) get watched as a separate bucket.
CLOSE_GAME_BAND = 0.05


def load_adaptive_settings():
    return safe_read_json(ADAPTIVE_SETTINGS_FILE, {})


def save_adaptive_settings(settings):
    atomic_write_json(ADAPTIVE_SETTINGS_FILE, settings)


def get_effective_favorite_min_prob():
    """
    The currently-learned "this counts as a real favorite" bar. Starts at
    MONEYLINE_FAVORITE_MIN_PROB_DEFAULT and only ever gets raised (never
    lowered automatically) once maybe_adjust_moneyline_favorite_threshold
    finds real evidence that picks near the old bar were too close to call.
    """
    return load_adaptive_settings().get("moneyline_favorite_min_prob", MONEYLINE_FAVORITE_MIN_PROB_DEFAULT)


def maybe_adjust_btc_momentum_window(send_discord_fn=None, webhook=None):
    """
    The REAL version of this function -- previously it only logged a win
    rate and wrote a "btc_momentum_window" number that nothing ever read
    back, so BTC always traded on a hardcoded 3-reading window no matter
    what this said. Now it actually does what its name claims:

    For every resolved BTC pick, its `price_snapshot` (saved at pick time)
    lets us retroactively ask "what would each candidate window (2, 3, 5)
    have predicted here?" and check that against the real outcome
    (`actual_direction`). That gives a genuine win rate per window, over
    the SAME set of real outcomes -- not a hypothetical.

    Only switches away from the current window if a candidate has both:
      (a) a real sample of its own (>= MIN_SAMPLE_FOR_ADJUSTMENT scoreable
          picks), and
      (b) a win rate significantly better than the current window's, using
          the same not-just-noise check as the moneyline threshold.
    Never switches on a tie or a marginal, could-be-noise difference.
    """
    paper_data = load_paper_trades()
    resolved = [
        t for t in paper_data["btc"]
        if t["status"] in ("won", "lost") and t.get("price_snapshot") and t.get("actual_direction")
    ]

    settings = load_adaptive_settings()
    current_window = settings.get("btc_momentum_window", BTC_MOMENTUM_WINDOW_DEFAULT)

    if len(resolved) < MIN_SAMPLE_FOR_ADJUSTMENT:
        settings["btc_momentum_window"] = current_window
        settings["btc_sample_size"] = len(resolved)
        save_adaptive_settings(settings)
        return None  # not enough scoreable history yet -- do nothing

    def outcomes_for_window(w):
        """1.0 per pick where this window would have called it right, else 0.0 -- skips picks whose snapshot isn't long enough for this window."""
        out = []
        for t in resolved:
            snap = t["price_snapshot"]
            if len(snap) <= w:
                continue
            momentum = snap[-1] - snap[-1 - w]
            predicted = "up" if momentum > 0 else "down"
            out.append(1.0 if predicted == t["actual_direction"] else 0.0)
        return out

    per_window = {w: outcomes_for_window(w) for w in BTC_MOMENTUM_WINDOW_CANDIDATES}
    current_outcomes = per_window.get(current_window, outcomes_for_window(current_window))

    settings["btc_sample_size"] = len(resolved)
    settings["btc_win_rate"] = statistics.mean(current_outcomes) if current_outcomes else None
    settings["btc_window_win_rates"] = {
        str(w): (round(statistics.mean(o), 4) if o else None) for w, o in per_window.items()
    }

    best_window, best_outcomes = current_window, current_outcomes
    for w, outcomes in per_window.items():
        if w == current_window or len(outcomes) < MIN_SAMPLE_FOR_ADJUSTMENT:
            continue
        if not current_outcomes:
            continue
        # "candidate beats current" as a paired difference: candidate_win - current_win
        # per matched pick where both windows could score it, so this compares
        # like-for-like rather than two differently-sized samples in isolation.
        diffs = []
        for t in resolved:
            snap = t["price_snapshot"]
            if len(snap) <= w or len(snap) <= current_window:
                continue
            cand_pred = "up" if (snap[-1] - snap[-1 - w]) > 0 else "down"
            cur_pred = "up" if (snap[-1] - snap[-1 - current_window]) > 0 else "down"
            cand_hit = 1.0 if cand_pred == t["actual_direction"] else 0.0
            cur_hit = 1.0 if cur_pred == t["actual_direction"] else 0.0
            diffs.append(cand_hit - cur_hit)

        if len(diffs) >= MIN_SAMPLE_FOR_ADJUSTMENT and statistics.mean(diffs) > 0:
            # Reuse the same "beats noise" check, just on the improvement margin
            # instead of a P&L total -- true only if the candidate's edge over
            # the current window survives giving it the benefit of the doubt.
            neg_diffs = [-d for d in diffs]
            if _mean_is_significantly_negative(neg_diffs, z=2.0):
                best_window, best_outcomes = w, outcomes

    if best_window != current_window:
        settings["btc_momentum_window"] = best_window
        settings["last_adjusted"] = datetime.now().isoformat()
        save_adaptive_settings(settings)
        if send_discord_fn and webhook:
            old_rate = statistics.mean(current_outcomes) * 100 if current_outcomes else 0
            new_rate = statistics.mean(best_outcomes) * 100 if best_outcomes else 0
            send_discord_fn(
                webhook,
                f"[LEARNING] BTC momentum window {current_window}->{best_window}: "
                f"{old_rate:.0f}% -> {new_rate:.0f}% win rate over {len(resolved)} resolved picks, "
                f"a large enough edge to trust. Switching."
            )
        return settings

    settings["btc_momentum_window"] = current_window
    save_adaptive_settings(settings)
    return settings


def maybe_adjust_moneyline_favorite_threshold(send_discord_fn=None, webhook=None):
    """
    The "learning" half of moneyline paper trading: once there's a real
    sample, checks whether picks sitting just above the current favorite
    bar (the "close" bucket -- too close to call, even though they cleared
    FAVORITE_MIN_PROB) are actually losing money, separately from clearer
    favorites further above the bar. If the close bucket has its own real
    sample size AND a hypothetical P&L that's negative by more than noise
    could explain (see _mean_is_significantly_negative), raises the bar so
    future picks skip that zone -- this is how it "notices" a favorite
    wasn't safe enough. Only ever tightens the bar, never loosens it on its
    own (loosening on noise is exactly the kind of mistake this is meant to
    avoid). Below MIN_SAMPLE_FOR_ADJUSTMENT resolved picks total, or below
    it for the close bucket specifically, this is a no-op.
    """
    paper_data = load_paper_trades()
    resolved = [
        t for t in paper_data["moneyline"]
        if t["status"] in ("won", "lost") and t.get("hypothetical_pnl") is not None
    ]
    if len(resolved) < MIN_SAMPLE_FOR_ADJUSTMENT:
        return None

    current_bar = get_effective_favorite_min_prob()
    close_band_top = round(current_bar + CLOSE_GAME_BAND, 4)

    close_bucket = [t for t in resolved if current_bar <= t["market_probability"] < close_band_top]
    clear_bucket = [t for t in resolved if t["market_probability"] >= close_band_top]

    settings = load_adaptive_settings()
    settings["moneyline_favorite_min_prob"] = current_bar
    settings["moneyline_sample_size"] = len(resolved)
    settings["moneyline_close_bucket_size"] = len(close_bucket)
    settings["moneyline_clear_bucket_size"] = len(clear_bucket)
    if clear_bucket:
        settings["moneyline_clear_bucket_pnl"] = round(sum(t["hypothetical_pnl"] for t in clear_bucket), 4)
        settings["moneyline_clear_bucket_win_rate"] = sum(1 for t in clear_bucket if t["status"] == "won") / len(clear_bucket)

    if len(close_bucket) >= MIN_SAMPLE_FOR_ADJUSTMENT:
        close_pnls = [t["hypothetical_pnl"] for t in close_bucket]
        close_pnl = round(sum(close_pnls), 4)
        close_win_rate = sum(1 for t in close_bucket if t["status"] == "won") / len(close_bucket)
        settings["moneyline_close_bucket_pnl"] = close_pnl
        settings["moneyline_close_bucket_win_rate"] = close_win_rate

        if close_band_top > current_bar and _mean_is_significantly_negative(close_pnls, z=2.0):
            settings["moneyline_favorite_min_prob"] = close_band_top
            settings["last_adjusted"] = datetime.now().isoformat()
            save_adaptive_settings(settings)
            if send_discord_fn and webhook:
                send_discord_fn(
                    webhook,
                    f"[LEARNING] Favorites in the {current_bar*100:.0f}-{close_band_top*100:.0f}% fair-odds range "
                    f"went {close_win_rate*100:.0f}% win rate (${close_pnl:+.2f} over {len(close_bucket)} picks) -- "
                    f"a large enough loss to trust, not just a bad run. Raising the favorite bar to {close_band_top*100:.0f}%."
                )
            return settings

    save_adaptive_settings(settings)
    return settings
