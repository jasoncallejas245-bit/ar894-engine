import os
import statistics
import requests
import uuid
from datetime import datetime, timezone

from state_io import atomic_write_json, safe_read_json
import context_data

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
PAPER_TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.json")
PAPER_BANKROLL_FILE = os.path.join(DATA_DIR, "paper_bankroll.json")

# Every paper trade sizes itself like a real bet of this size, instead of
# the old "1 contract at entry price" math -- so the logged hypothetical
# P&L reflects what would actually happen if this pick had been placed
# for real at the stake size this bot actually uses. Raised from $5 to
# $15 on 2026-09-10 at the user's request -- $5 winnings looked too
# small to read as meaningful on the dashboard; $15+ makes the paper P&L
# numbers actually informative.
PAPER_STAKE_DOLLARS = float(os.getenv("PAPER_STAKE_DOLLARS", "15.0"))

# Starting notional bankroll per category, purely for tracking "are we up or
# down against a hypothetical budget" over time -- has no bearing on real
# money, just makes the paper data readable as a running balance.
PAPER_STARTING_BANKROLL = float(os.getenv("PAPER_STARTING_BANKROLL", "100.0"))


def load_paper_bankroll():
    return safe_read_json(PAPER_BANKROLL_FILE, {
        "moneyline": {"balance": PAPER_STARTING_BANKROLL, "history": []},
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
    data = safe_read_json(PAPER_TRADES_FILE, {"moneyline": []})
    data.setdefault("parlay", [])
    data.setdefault("props", [])
    data.setdefault("passing_yards", [])
    data.setdefault("manual_bets", [])
    data.setdefault("wnba_combined", [])
    return data


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
            "contract_price_history": [],
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


# UFC/ATP aren't daily leagues -- a fight card or tournament match is
# scheduled days ahead, not same-day, so the usual same-day-only pick
# window (below) would throw out every upcoming event until its exact
# day arrives. These get a longer lookahead instead. 8 days covers a
# normal fight-week/tournament-week gap without reaching so far ahead
# that lines are still soft/thin.
MULTI_DAY_LOOKAHEAD_LEAGUES = {"ufc", "atp"}
MULTI_DAY_LOOKAHEAD_HOURS = float(os.getenv("MULTI_DAY_LOOKAHEAD_HOURS", "192"))  # 8 days


def make_moneyline_paper_picks(league, sharpapi_rows, kalshi_events, safe_match_fn, send_discord_fn, webhook, min_edge_pct=2.0, favorite_min_prob=None, real_min_edge_pct=None, real_favorite_min_prob=None):
    """
    Mirrors the REAL trading edge-detection logic exactly (checks both YES
    and NO for a genuine mispricing edge, skips the game entirely if
    neither side clears the bar, and only backs the side actually favored
    to win -- same FAVORITE_MIN_PROB bar real trading uses) -- so paper
    trading actually validates the same method used for real money, just
    extended to more sports and with no cap on how many picks it can make.

    min_edge_pct/favorite_min_prob control which picks get made AT ALL
    here (usually a wide, data-collection net). real_min_edge_pct/
    real_favorite_min_prob are optional and separate -- when passed
    (worker.py passes the actual real-trading bar, MIN_EDGE_PCT and
    get_favorite_min_prob()), each pick is additionally flagged
    "manual_bet_candidate": True if it ALSO would have cleared that
    tighter real-trading bar, so the dashboard can show "this one's just
    data" vs. "this one's good enough that the bot itself would have bet
    it for real, if real trading were on for this league." Flag is None
    (not evaluated) if either real_* threshold isn't passed in.
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

        # Same-day only for daily leagues (MLB/NBA/NFL/NCAAF/WNBA) -- mirrors
        # real trading: the event must start later today (UTC), not some
        # rolling window that could roll into tomorrow, so picks don't pile
        # up for games that can't resolve soon.
        #
        # UFC/ATP are NOT daily -- a UFC card or an ATP tournament match is
        # scheduled days ahead and shows up in SharpAPI/Kalshi well before
        # fight/match day. Confirmed live (2026-09-10): every one of 29
        # upcoming UFC fights was being thrown out here every single cycle
        # because none of them started "today" yet -- the bot would only
        # ever get a shot at picking a card on its exact fight day, and if
        # that day's scan missed for any reason, the whole card was gone.
        # So these leagues get a longer lookahead instead of same-day-only.
        start_str = best_row.get("event_start_time")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception:
            continue
        now = datetime.now(timezone.utc)
        hours_until = (start_dt - now).total_seconds() / 3600
        if league in MULTI_DAY_LOOKAHEAD_LEAGUES:
            in_window = 0 <= hours_until <= MULTI_DAY_LOOKAHEAD_HOURS
        else:
            in_window = hours_until >= 0 and start_dt.date() == now.date()
        if not in_window:
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
        # Was close-call-only; now pulled for every pick at the user's
        # request (2026-09-09) -- they check weather/injuries/matchups/
        # pitcher on every bet they place themselves, not just coin-flip
        # ones, so Picks' picks now carry the same context regardless of
        # how confident the edge looked.
        context_note = None
        try:
            context_note = context_data.get_context_note(league, away_team, home_team)
        except Exception as e:
            print(f"[context_data] lookup failed for {away_team} @ {home_team}: {e}")

        manual_bet_candidate = None
        if real_min_edge_pct is not None and real_favorite_min_prob is not None:
            manual_bet_candidate = (
                _clears_fee_adjusted_edge_local(best_pick["edge_pct"], best_pick["entry_price"], real_min_edge_pct)
                and best_pick["market_probability"] >= real_favorite_min_prob
            )

        # bet_tier ranks "bang for your buck" instead of a hard yes/no gate.
        # "skip" is reserved for picks with essentially zero or negative
        # detected edge (the wide, data-collection-only net lets these
        # through on purpose to build sample size) -- those are the only
        # ones actually worth steering away from. Everything with real
        # positive edge stays visible and ranked, even below the strict
        # real-trading bar, so nothing useful gets hidden.
        edge = best_pick["edge_pct"] or 0.0
        if edge <= 0:
            bet_tier = "skip"
        elif manual_bet_candidate:
            bet_tier = "strong"
        else:
            bet_tier = "thin"

        # pick_score: a 0-100 "bang for your buck" gauge for the confidence
        # bar on the dashboard -- continuous, not just the 3-way tier, so
        # picks within a tier can still be told apart at a glance. Half
        # from edge size (8%+ edge treated as excellent), half from how far
        # above a coinflip the market's own probability is (30pts of prob
        # range = full marks). Clamped 0-100.
        _prob = best_pick["market_probability"] or 0.5
        _edge_component = max(0.0, min(1.0, edge / 8.0)) * 100
        _prob_component = max(0.0, min(1.0, (_prob - 0.50) / 0.30)) * 100
        pick_score = round((_edge_component + _prob_component) / 2)

        pick = {
            "pick_id": uuid.uuid4().hex[:12],
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
            "contract_price_history": [],
            "event_start_time": start_str,
            "manual_bet_candidate": manual_bet_candidate,
            "bet_tier": bet_tier,
            "pick_score": pick_score,
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
            # Fixed-stake simulation: same sizing math real trading uses
            # (stake_dollars / price, minimum 1 contract), not the old
            # "1 contract at entry_price" model, so the hypothetical P&L
            # reflects what a real bet of this size on this pick would have made.
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


# Bound how much contract-price history one pending moneyline pick keeps.
MONEYLINE_CONTRACT_HISTORY_MAX = 30


def track_moneyline_contract_prices(client):
    """
    Runs on the fast cycle for every still-PENDING moneyline paper pick
    (pregame AND live -- both are always side="YES", see
    make_moneyline_paper_picks / record_live_moneyline_pick): records the
    Kalshi contract's current yes-side bid, building a real price path for
    the life of the trade. Data collection only -- never closes a
    position. Never raises.
    """
    try:
        paper_data = load_paper_trades()
        pending = [p for p in paper_data["moneyline"] if p["status"] == "pending" and p.get("kalshi_ticker")]
        if not pending:
            return

        changed = False
        now_iso = datetime.now().isoformat()
        for pick in pending:
            try:
                market = client.get_market(pick["kalshi_ticker"])
            except Exception:
                continue

            bid = getattr(market, "yes_bid_dollars", None)
            if not bid:
                continue

            pick.setdefault("contract_price_history", [])
            pick["contract_price_history"].append({"at": now_iso, "price": float(bid)})
            pick["contract_price_history"] = pick["contract_price_history"][-MONEYLINE_CONTRACT_HISTORY_MAX:]
            changed = True

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] track_moneyline_contract_prices error: {e}")


# Closes a paper pick early once its tracked bid crosses
# MONEYLINE_PAPER_EARLY_EXIT_PROB, instead of waiting for the game to
# finish. Not backed by a tracked price-path backtest yet -- there's no
# historical moneyline contract_price_history to test against (that data
# only starts being collected once this ships). Treat this as an
# untested experiment: paper-only, tunable/disable-able via env, and its
# own real performance will show up in contract_price_history + the
# dashboard once picks resolve this way.
# Re-enabled (2026-09-10, user request), redesigned around CAPTURED
# PROFIT rather than a flat contract price. A flat price threshold (the
# original 92%, then a tried-and-rejected 97%) doesn't mean the same
# thing for every pick: a $15 bet at $0.50/contract (30 contracts) has
# $15 of profit on the table if it resolves, while the same $15 bet at
# $0.85/contract only has ~$2.65 on the table -- a flat price cutoff
# either exits the first one too early or the second one too late.
#
# Per the user's own example: bet $15, it could pay out $30 total ($15
# profit) if held to resolution -- don't cash out at $17 (only $2 of that
# $15 captured, 13%), but DO cash out at $25 ($10 captured, 67%) rather
# than risk the whole position for the last $5. So this now exits once
# MONEYLINE_PAPER_EARLY_EXIT_PROFIT_RATIO of the MAX POSSIBLE profit
# (from entry price to $1.00) is already locked in -- 0.67 by default,
# which is exactly the $25-of-$30 example above.
MONEYLINE_PAPER_EARLY_EXIT_ENABLED = os.getenv("MONEYLINE_PAPER_EARLY_EXIT_ENABLED", "true").lower() == "true"
MONEYLINE_PAPER_EARLY_EXIT_PROFIT_RATIO = float(os.getenv("MONEYLINE_PAPER_EARLY_EXIT_PROFIT_RATIO", "0.67"))


def check_and_close_moneyline_paper_early(send_discord_fn=None, webhook=None):
    """
    Runs on the fast cycle right after track_moneyline_contract_prices. If
    a pending pick has already captured MONEYLINE_PAPER_EARLY_EXIT_PROFIT_RATIO
    of its maximum possible profit (entry price to $1.00), closes it out
    AT THE LATEST TRACKED PRICE instead of waiting for the game to finish
    (both entry and exit taker fees charged). Paper-only; never touches a
    real position. Never raises.
    """
    if not MONEYLINE_PAPER_EARLY_EXIT_ENABLED:
        return
    try:
        paper_data = load_paper_trades()
        changed = False
        for pick in paper_data["moneyline"]:
            if pick["status"] != "pending":
                continue
            history = pick.get("contract_price_history") or []
            if not history or not pick.get("entry_price"):
                continue
            latest = history[-1]["price"]
            entry_price = pick["entry_price"]
            if entry_price >= 1.0:
                continue
            max_possible_profit_per_contract = 1.0 - entry_price
            captured_profit_per_contract = latest - entry_price
            captured_ratio = captured_profit_per_contract / max_possible_profit_per_contract
            if captured_ratio < MONEYLINE_PAPER_EARLY_EXIT_PROFIT_RATIO:
                continue

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
                "moneyline", pnl, pick.get("kalshi_ticker"),
                note=f"{pick['league']} {pick['picked_team']} early-exit at ${latest:.2f}",
            )
            if send_discord_fn and webhook:
                send_discord_fn(
                    webhook,
                    f"[PAPER EARLY EXIT - {pick['league']}] {pick['picked_team']} cashed out at ${latest:.2f} "
                    f"(entry ${entry_price:.2f}) -- ${pnl:+.2f}. Moneyline paper bankroll: ${balance:.2f}",
                )

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] check_and_close_moneyline_paper_early error: {e}")


def get_paper_trade_summary():
    paper_data = load_paper_trades()
    summary = {}
    for category in ["moneyline", "parlay", "props", "passing_yards", "wnba_combined"]:
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
        # Still record progress toward the threshold -- previously this returned
        # without ever writing moneyline_sample_size, so the dashboard's
        # "Data collected" bar showed 0 of 30 the entire time, even once
        # real resolved bets existed. Fixed 2026-09-09.
        settings = load_adaptive_settings()
        settings["moneyline_sample_size"] = len(resolved)
        save_adaptive_settings(settings)
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


# ---------------------------------------------------------------------------
# Parlay paper trading (NEW 2026-09-09, at the user's explicit request)
#
# IMPORTANT LIMITATION, stated up front: Kalshi has no parlay/combo-bet
# product at all -- every Kalshi market is a single, independent yes/no
# contract. This mode can NEVER become a real-money trading path on
# Kalshi, no matter how good the paper results look. It exists purely to
# test, on paper, whether the user's own manual approach (stacking 3+
# moneyline favorites into one combined ticket for a bigger payout
# multiplier, the pattern found in their Gemini betting history) actually
# outperforms Picks' normal one-position-at-a-time approach. If this
# strategy is ever worth acting on for real, it would have to be through a
# platform that actually supports parlays (e.g. PrizePicks), not this bot.
# ---------------------------------------------------------------------------

PARLAY_ENABLED = os.getenv("PARLAY_PAPER_ENABLED", "true").lower() == "true"
# Changed 2026-09-10, at the user's request: instead of ALWAYS bundling
# exactly 3 legs, build one ticket for EVERY leg count in this list each
# cycle (when there are enough eligible legs), reusing the same ranked
# candidate pool -- a 2-leg ticket is just the top 2, a 3-leg ticket is
# the top 2 plus the next-best leg, and so on. Same picks, different
# combos, so leg-count profitability can be compared directly on real
# data instead of guessed at (see get_parlay_leg_count_breakdown /
# the dashboard's parlay chart) -- basic parlay math says win
# probability multiplies down fast as legs are added (each extra leg is
# another independent chance to bust), so more legs is a real bet that
# the bigger payout multiplier is worth the much lower hit rate. Data
# answers that, not a guess.
PARLAY_LEG_COUNTS = [int(n) for n in os.getenv("PARLAY_LEG_COUNTS", "2,3,4,5,6").split(",") if n.strip()]
# Mirrors a real lesson from the user's own betting history: a leg priced
# heavier than this (e.g. a -300 favorite, ~0.75+ on Kalshi) eats up parlay
# payout value without adding much safety, so it gets skipped in favor of
# the next-best qualifying leg instead.
PARLAY_MAX_LEG_PRICE = float(os.getenv("PARLAY_MAX_LEG_PRICE", "0.75"))


def maybe_make_parlay_pick(candidate_picks, send_discord_fn=None, webhook=None):
    """
    Runs once per sports-scan cycle in worker.py, fed that cycle's newly
    made single-position picks (across every league) as candidate_picks.
    Builds ONE ticket per leg count in PARLAY_LEG_COUNTS that has enough
    qualifying legs (real edge, not too heavy a favorite -- see
    PARLAY_MAX_LEG_PRICE), each tracked in the same "parlay" bankroll but
    tagged with its own leg_count so a 2-leg strategy and a 6-leg
    strategy can be judged separately. Legs are reused across ticket
    sizes (same ranked pool, different cutoffs) -- completely separate
    from those same picks' own single-position tracking, which continues
    exactly as before. One picked game can appear in a single position
    AND several parlay legs at once; they're independent records
    answering different questions. Returns the list of tickets made this
    cycle (may be empty). Never raises.
    """
    if not PARLAY_ENABLED or not PARLAY_LEG_COUNTS:
        return []
    try:
        eligible = [
            p for p in candidate_picks
            if p.get("kalshi_ticker") and p.get("entry_price") and p["entry_price"] <= PARLAY_MAX_LEG_PRICE
        ]
        eligible.sort(key=lambda p: p.get("edge_pct") or 0, reverse=True)

        tickets = []
        paper_data = load_paper_trades()
        for leg_count in sorted(set(PARLAY_LEG_COUNTS)):
            if len(eligible) < leg_count:
                continue
            legs = eligible[:leg_count]

            combined_price = 1.0
            for leg in legs:
                combined_price *= leg["entry_price"]
            combined_price = max(0.0001, combined_price)

            contracts = max(1.0, PAPER_STAKE_DOLLARS / combined_price)
            entry_fees = sum(_kalshi_taker_fee_dollars_local(leg["entry_price"], contracts) for leg in legs)

            ticket = {
                "ticket_id": f"parlay-{leg_count}leg-{datetime.now().isoformat()}",
                "leg_count": leg_count,
                "legs": [
                    {
                        "league": leg["league"], "picked_team": leg["picked_team"],
                        "kalshi_ticker": leg["kalshi_ticker"], "event_id": leg.get("event_id"),
                        "entry_price": leg["entry_price"], "edge_pct": leg.get("edge_pct"),
                    }
                    for leg in legs
                ],
                "combined_entry_price": round(combined_price, 6),
                "contracts": contracts,
                "entry_fees": round(entry_fees, 4),
                "stake_dollars": round(combined_price * contracts, 4),
                "picked_at": datetime.now().isoformat(),
                "status": "pending",
            }
            paper_data.setdefault("parlay", []).append(ticket)
            tickets.append(ticket)

        if not tickets:
            return []

        save_paper_trades(paper_data)

        if send_discord_fn and webhook:
            lines = [f"[PAPER PARLAY] {len(tickets)} new ticket(s) this cycle (2-6 leg combos, same candidate pool):"]
            for ticket in tickets:
                potential = round(ticket["contracts"] * (1.0 - ticket["combined_entry_price"]), 2)
                leg_names = ", ".join(f"{l['picked_team']} ({l['league']})" for l in ticket["legs"])
                lines.append(f"  {ticket['leg_count']}-leg, ${ticket['stake_dollars']:.2f} to win ${potential:+.2f}: {leg_names}")
            lines.append("(Paper only -- Kalshi has no real parlay product, this is comparison data only)")
            send_discord_fn(webhook, "\n".join(lines))
        return tickets
    except Exception as e:
        print(f"[paper_trading] maybe_make_parlay_pick error: {e}")
        return []


def get_parlay_leg_count_breakdown():
    """
    Groups resolved parlay tickets by leg_count so it's clear which
    ticket size is actually profitable on paper, not just guessed at.
    Tickets from before leg_count was tracked (2026-09-10) are grouped
    under leg_count 3, the old fixed size, since that's what they were.
    Returns a list of dicts sorted by leg_count, each with resolved
    count, win rate, and total hypothetical P&L.
    """
    paper_data = load_paper_trades()
    by_count = {}
    for t in paper_data.get("parlay", []):
        if t["status"] not in ("won", "lost"):
            continue
        lc = t.get("leg_count", 3)
        by_count.setdefault(lc, {"resolved": 0, "wins": 0, "pnl": 0.0})
        by_count[lc]["resolved"] += 1
        if t["status"] == "won":
            by_count[lc]["wins"] += 1
        by_count[lc]["pnl"] += t.get("hypothetical_pnl") or 0.0

    return [
        {
            "leg_count": lc,
            "resolved": v["resolved"],
            "win_rate": (v["wins"] / v["resolved"] * 100) if v["resolved"] else None,
            "total_pnl": round(v["pnl"], 2),
        }
        for lc, v in sorted(by_count.items())
    ]


def resolve_parlay_paper_trades(send_discord_fn=None, webhook=None):
    """
    Checks each pending parlay ticket's legs against that same cycle's
    already-resolved single-position moneyline picks (matched by
    kalshi_ticker) -- an all-or-nothing parlay: every leg has to have
    resolved "won" for the ticket to win; any leg "lost" busts the whole
    ticket, same as a real sportsbook parlay. Never raises.
    """
    try:
        paper_data = load_paper_trades()
        moneyline_by_ticker = {p.get("kalshi_ticker"): p for p in paper_data.get("moneyline", []) if p.get("kalshi_ticker")}
        changed = False

        for ticket in paper_data.get("parlay", []):
            if ticket["status"] != "pending":
                continue

            leg_statuses = []
            for leg in ticket["legs"]:
                underlying = moneyline_by_ticker.get(leg["kalshi_ticker"])
                leg_statuses.append(underlying["status"] if underlying else None)

            if any(s is None for s in leg_statuses):
                continue  # a leg's underlying pick vanished -- can't resolve, leave pending
            if any(s == "pending" for s in leg_statuses):
                continue  # still waiting on at least one leg

            won = all(s == "won" for s in leg_statuses)
            ticket["status"] = "won" if won else "lost"
            ticket["resolved_at"] = datetime.now().isoformat()

            if won:
                gross = ticket["contracts"] * (1.0 - ticket["combined_entry_price"])
                pnl = round(gross - ticket["entry_fees"], 4)
            else:
                pnl = round(-ticket["stake_dollars"] - ticket["entry_fees"], 4)
            ticket["hypothetical_pnl"] = pnl
            changed = True

            balance, is_down, down_by = record_paper_bankroll_change(
                "parlay", pnl, ticket["ticket_id"],
                note=f"{len(ticket['legs'])}-leg parlay {ticket['status']}",
            )
            if send_discord_fn and webhook:
                send_discord_fn(
                    webhook,
                    f"[PARLAY {'HIT' if won else 'BUSTED'}] {len(ticket['legs'])}-leg ticket: ${pnl:+.2f}. "
                    f"Parlay paper bankroll: ${balance:.2f}",
                )

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] resolve_parlay_paper_trades error: {e}")


# ---------------------------------------------------------------------------
# PrizePicks-style player prop picks (NEW 2026-09-09, at the user's request)
#
# HONESTY UP FRONT: PrizePicks has no public API at all -- there's no free
# way to pull PrizePicks' own exact lines. This uses SharpAPI's own
# player-prop consensus lines instead (a market SharpAPI's site confirms
# it supports, already paid for and used elsewhere in this bot -- zero
# new cost). The real question being tested -- "does picking the side
# sportsbooks lean hardest toward actually win more than a coinflip" --
# is the same real test either way, but PrizePicks' literal number for a
# given player might differ slightly from the consensus line used here.
# If this proves out on real data, upgrading to PrizePicks' actual lines
# would need a paid third-party feed -- NOT added without asking first.
#
# Grading is never a guess (see context_data.get_player_boxscore_stat):
# MLB is confirmed live against a real box score. NBA/WNBA are included
# but UNVERIFIED -- same safe-fail approach as adding NBA to moneyline.
# NFL/NCAAF are deliberately left out for now -- ESPN's box score can
# list more than one "yards" stat per player (passing/rushing/receiving)
# and getting that wrong would silently grade a pick incorrectly, which
# is worse than not grading it at all.
# ---------------------------------------------------------------------------
PROP_PICKS_ENABLED = os.getenv("PROP_PICKS_PAPER_ENABLED", "true").lower() == "true"
PROP_LEG_COUNT = int(os.getenv("PROP_LEG_COUNT", "4"))
PROP_MIN_CONSENSUS_PROB = float(os.getenv("PROP_MIN_CONSENSUS_PROB", "0.55"))
# NFL/NCAAF added 2026-09-10, at the user's request (they specifically
# want passing-yards picks) -- previously excluded over a real concern
# that ESPN's box score could mix up passing/rushing/receiving yards,
# but that's now checked live: ESPN reliably separates those into
# distinct named stat groups, so context_data.get_player_boxscore_stat
# now matches the group name too, not just the label. What's still
# unverified is SharpAPI's exact stat_category string for NFL props
# (e.g. whether it's "passing_yards" or something else) -- same
# safe-fail approach as everywhere else: wrong guess just means
# "needs_manual_check," never a wrong grade.
PROP_GRADABLE_LEAGUES = {"mlb", "nba", "wnba", "nfl", "ncaaf"}


def _parse_player_prop_row(row):
    """
    Extraction from one SharpAPI player-prop row. Field names CONFIRMED
    2026-09-10 against a real live response (market="props" -- the
    OpenAPI-spec-derived guesses "player_prop"/"selection"/"probability"
    turned out wrong; a live probe against the real API found the actual
    working value and a real sample row, see worker.probe_sharpapi_player_prop_market):
    player_name, stat_category, line, selection_type ("over"/"under",
    already lowercase), odds_probability -- plus event_id/away_team/
    home_team/sportsbook, same as the main-line moneyline rows. Older
    guessed names kept as a fallback only. Returns None rather than
    guessing if it can't confidently parse the row.
    """
    try:
        player = row.get("player_name") or row.get("player") or row.get("athlete")
        stat = row.get("stat_category") or row.get("stat_type") or row.get("prop_type")
        line = row.get("line")
        if line is None:
            line = row.get("point")
        side = row.get("selection_type") or row.get("selection") or row.get("side")
        prob = row.get("odds_probability")
        if prob is None:
            prob = row.get("probability")
        if not player or not stat or line is None or not side:
            return None
        side_norm = str(side).strip().lower()
        if side_norm not in ("over", "under"):
            return None
        return {
            "player": str(player).strip(),
            "stat_type": str(stat).strip().lower(),
            "line": float(line),
            "side": side_norm,
            "prob": float(prob) if prob is not None else None,
            "sportsbook": row.get("sportsbook"),
            "event_id": row.get("event_id"),
            "away_team": row.get("away_team"),
            "home_team": row.get("home_team"),
            "is_alternate_line": bool(row.get("is_alternate_line")),
        }
    except Exception:
        return None


def maybe_make_prop_pick(league, prop_rows, send_discord_fn=None, webhook=None):
    """
    Runs once per cycle per gradable league, fed that league's raw
    SharpAPI player-prop rows. Groups parsed rows by (player, stat_type,
    line, side), averages the implied probability across whichever
    sportsbooks quote it, keeps only sides clearing PROP_MIN_CONSENSUS_PROB,
    picks the strongest PROP_LEG_COUNT (one leg per player, so a ticket
    reads like a real PrizePicks slip -- different players, not the same
    guy twice), and paper-tracks them as one all-or-nothing ticket, same
    structure as parlay mode. Never raises.
    """
    if not PROP_PICKS_ENABLED or league not in PROP_GRADABLE_LEAGUES:
        return None
    try:
        parsed = [p for p in (_parse_player_prop_row(r) for r in prop_rows) if p]
        if not parsed:
            return None

        groups = {}
        for p in parsed:
            key = (p["player"], p["stat_type"], p["line"], p["side"])
            groups.setdefault(key, []).append(p)

        candidates = []
        for (player, stat_type, line, side), rows in groups.items():
            probs = [r["prob"] for r in rows if r["prob"] is not None]
            if not probs:
                continue
            avg_prob = sum(probs) / len(probs)
            if avg_prob < PROP_MIN_CONSENSUS_PROB:
                continue
            sample = rows[0]
            candidates.append({
                "player": player, "stat_type": stat_type, "line": line, "side": side,
                "consensus_prob": round(avg_prob, 4), "book_count": len(probs),
                "event_id": sample.get("event_id"), "away_team": sample.get("away_team"),
                "home_team": sample.get("home_team"), "league": league,
            })

        if len(candidates) < PROP_LEG_COUNT:
            return None

        candidates.sort(key=lambda c: c["consensus_prob"], reverse=True)
        legs, used_players = [], set()
        for c in candidates:
            if c["player"] in used_players:
                continue
            legs.append(c)
            used_players.add(c["player"])
            if len(legs) == PROP_LEG_COUNT:
                break
        if len(legs) < PROP_LEG_COUNT:
            return None

        for leg in legs:
            if not leg.get("event_id"):
                leg["event_id"] = context_data.get_event_id_for_matchup(league, leg.get("away_team"), leg.get("home_team"))

        ticket = {
            "ticket_id": f"prop-{datetime.now().isoformat()}",
            "league": league,
            "legs": legs,
            "stake_dollars": PAPER_STAKE_DOLLARS,
            "picked_at": datetime.now().isoformat(),
            "status": "pending",
        }
        paper_data = load_paper_trades()
        paper_data.setdefault("props", []).append(ticket)
        save_paper_trades(paper_data)

        if send_discord_fn and webhook:
            leg_lines = "\n".join(
                f"  - {l['player']} {l['side'].upper()} {l['line']} {l['stat_type']} ({l['consensus_prob']*100:.0f}%)"
                for l in legs
            )
            send_discord_fn(
                webhook,
                f"[PAPER PRIZEPICKS-STYLE] New {len(legs)}-leg {league.upper()} ticket:\n{leg_lines}\n"
                f"(Paper only -- uses sportsbook consensus lines, not PrizePicks' own numbers -- see code comments)"
            )
        return ticket
    except Exception as e:
        print(f"[paper_trading] maybe_make_prop_pick error: {e}")
        return None


def resolve_prop_paper_trades(send_discord_fn=None, webhook=None):
    """
    Checks each pending prop ticket's legs against real final box-score
    stats once every leg's game is confirmed final. All-or-nothing, same
    as a real PrizePicks Power Play: every leg has to hit. If a leg can't
    be graded (game not final, player not found, unsupported stat), the
    WHOLE ticket is marked "needs_manual_check" rather than guessed at --
    unless another leg has ALREADY definitively missed, since one busted
    leg fails the ticket regardless of the others. Never raises.
    """
    try:
        paper_data = load_paper_trades()
        changed = False

        for ticket in paper_data.get("props", []):
            if ticket["status"] != "pending":
                continue

            league = ticket["league"]
            any_confirmed_miss = False
            any_ungradable = False
            hit_count = 0

            for leg in ticket["legs"]:
                event_id = leg.get("event_id")
                if not event_id or not context_data.is_game_final(league, event_id):
                    any_ungradable = True
                    continue
                value, found = context_data.get_player_boxscore_stat(league, event_id, leg["player"], leg["stat_type"])
                if not found:
                    any_ungradable = True
                    continue
                hit = (value > leg["line"]) if leg["side"] == "over" else (value < leg["line"])
                leg["actual_value"] = value
                leg["hit"] = hit
                if hit:
                    hit_count += 1
                else:
                    any_confirmed_miss = True

            if any_confirmed_miss:
                ticket["status"] = "lost"
            elif any_ungradable:
                # Could still be pending (games not final yet) or stuck
                # ungradable (bad player-name match, unsupported stat).
                # Leave it pending unless every leg's game is at least
                # final -- only then call it "needs_manual_check".
                all_final = all(
                    leg.get("event_id") and context_data.is_game_final(league, leg["event_id"])
                    for leg in ticket["legs"]
                )
                if all_final:
                    ticket["status"] = "needs_manual_check"
                else:
                    continue
            else:
                ticket["status"] = "won"

            ticket["resolved_at"] = datetime.now().isoformat()
            changed = True

            if ticket["status"] in ("won", "lost"):
                pnl = ticket["stake_dollars"] * 3 if ticket["status"] == "won" else -ticket["stake_dollars"]
                ticket["hypothetical_pnl"] = round(pnl, 2)
                # NOTE: the 3x payout above is a rough stand-in for a
                # real PrizePicks-style payout multiplier (varies by
                # leg count and pick type in real life) -- good enough
                # to track "up or down," not a promise of the real payout.
                balance, is_down, down_by = record_paper_bankroll_change(
                    "props", pnl, ticket["ticket_id"], note=f"{len(ticket['legs'])}-leg prop ticket {ticket['status']}",
                )
                if send_discord_fn and webhook:
                    send_discord_fn(
                        webhook,
                        f"[PRIZEPICKS-STYLE {'HIT' if ticket['status']=='won' else 'BUSTED'}] "
                        f"{len(ticket['legs'])}-leg {league.upper()} ticket: ${pnl:+.2f}. Props paper bankroll: ${balance:.2f}",
                    )
            elif send_discord_fn and webhook:
                send_discord_fn(
                    webhook,
                    f"[PRIZEPICKS-STYLE] {len(ticket['legs'])}-leg {league.upper()} ticket needs a manual check -- "
                    f"couldn't auto-grade every leg (unsupported stat or player-name mismatch).",
                )

        if changed:
            save_paper_trades(paper_data)
    except Exception as e:
        print(f"[paper_trading] resolve_prop_paper_trades error: {e}")


# ---------------------------------------------------------------------------
# NFL/NCAAF passing yards -- individual picks (NEW 2026-09-10, at the
# user's explicit request: passing yards should be a MAIN FOCUS, with
# high volume so a real track record builds fast, picked smartly (real
# sportsbook-consensus edge, not randomly). Unlike the 4-leg all-or-
# nothing PrizePicks-style ticket above -- which needs 4 qualifying legs
# across ANY stat/player simultaneously, and so barely ever fires --
# every qualifying passing-yards leg here is its OWN independent pick.
# It resolves on its own, same as a moneyline pick, so volume isn't
# bottlenecked by needing three unrelated players to also qualify the
# same cycle.
# ---------------------------------------------------------------------------
PASSING_YARDS_LEAGUES = {"nfl", "ncaaf"}
# Deliberately looser than PROP_MIN_CONSENSUS_PROB (0.55) -- the user
# wants volume here specifically, so this only filters out picks with
# no real lean at all, not picks that are merely less than 55% sure.
# Still real sportsbook-consensus edge, never a coinflip guess.
PASSING_YARDS_MIN_PROB = float(os.getenv("PASSING_YARDS_MIN_PROB", "0.52"))


def maybe_make_passing_yards_picks(league, prop_rows, send_discord_fn=None, webhook=None):
    """
    Runs once per cycle for nfl/ncaaf, fed that league's raw SharpAPI
    player-prop rows. For every quarterback's passing-yards line where
    one side (over/under) clears PASSING_YARDS_MIN_PROB on sportsbook
    consensus, makes ONE independent paper pick for that side -- no
    all-or-nothing bundling, so this can generate many picks per cycle,
    each graded on its own against the real box score. Skips a
    (player, line, event) that already has a pending pick, so it doesn't
    re-pick the same leg every cycle until the game finishes. Never
    raises.
    """
    if league not in PASSING_YARDS_LEAGUES:
        return []
    try:
        parsed = [
            p for p in (_parse_player_prop_row(r) for r in prop_rows)
            if p and p["stat_type"] == "passing_yards"
        ]
        if not parsed:
            return []

        groups = {}
        for p in parsed:
            key = (p["player"], p["line"], p["side"])
            groups.setdefault(key, []).append(p)

        paper_data = load_paper_trades()
        # Dedup on (player, event_id) only -- NOT line. A sportsbook's
        # passing-yards line for the same QB in the same game commonly
        # ticks by half a yard cycle to cycle; keying on the exact line
        # was creating a brand-new "pick" every time it moved instead of
        # recognizing this player/game already has one pending, which is
        # why pick counts were climbing far faster than actual games.
        already_picked = {
            (p["player"], p.get("event_id"))
            for p in paper_data["passing_yards"] if p["status"] == "pending"
        }

        # Pick the single SAFEST side/line per player -- across every line
        # a sportsbook offers (main line and any alternate/"goblin" lines
        # alike), not just over-vs-under on one line. Prefers whichever
        # has the highest consensus probability, so a safer alt line beats
        # a marginal main-line coinflip whenever one's available.
        by_player_line = {}
        for (player, line, side), rows in groups.items():
            probs = [r["prob"] for r in rows if r["prob"] is not None]
            if not probs:
                continue
            avg_prob = sum(probs) / len(probs)
            pl_key = player
            existing = by_player_line.get(pl_key)
            if existing is None or avg_prob > existing["consensus_prob"]:
                sample = rows[0]
                by_player_line[pl_key] = {
                    "player": player, "line": line, "side": side,
                    "consensus_prob": round(avg_prob, 4), "book_count": len(probs),
                    "event_id": sample.get("event_id"), "away_team": sample.get("away_team"),
                    "home_team": sample.get("home_team"),
                    "is_alternate_line": any(r.get("is_alternate_line") for r in rows),
                }

        new_picks = []
        for player, cand in by_player_line.items():
            line = cand["line"]
            if cand["consensus_prob"] < PASSING_YARDS_MIN_PROB:
                continue
            event_id = cand.get("event_id") or context_data.get_event_id_for_matchup(
                league, cand.get("away_team"), cand.get("home_team")
            )
            dedup_key = (player, event_id)
            if dedup_key in already_picked:
                continue

            pick = {
                "pick_id": uuid.uuid4().hex[:12],
                "player": player,
                "league": league.upper(),
                "stat_type": "passing_yards",
                "line": line,
                "side": cand["side"],
                "consensus_prob": cand["consensus_prob"],
                "book_count": cand["book_count"],
                "is_alternate_line": cand.get("is_alternate_line", False),
                "event_id": event_id,
                "away_team": cand.get("away_team"),
                "home_team": cand.get("home_team"),
                "entry_price": cand["consensus_prob"],
                "picked_at": datetime.now().isoformat(),
                "status": "pending",
                "bet_tier": "strong" if cand["consensus_prob"] >= 0.60 else "thin",
                "pick_score": round(max(0.0, min(1.0, (cand["consensus_prob"] - 0.50) / 0.20)) * 100),
            }
            paper_data["passing_yards"].append(pick)
            new_picks.append(pick)
            already_picked.add(dedup_key)

        if new_picks:
            save_paper_trades(paper_data)
            if send_discord_fn and webhook:
                lines = [f"[PAPER PASSING YARDS - {league.upper()}] {len(new_picks)} pick(s) this cycle:"]
                for p in new_picks:
                    lines.append(f"- {p['player']} {p['side'].upper()} {p['line']} passing yds ({p['consensus_prob']*100:.0f}%)")
                send_discord_fn(webhook, "\n".join(lines))

        return new_picks
    except Exception as e:
        print(f"[paper_trading] maybe_make_passing_yards_picks error: {e}")
        return []


def resolve_passing_yards_picks(send_discord_fn=None, webhook=None):
    """
    Checks each pending passing-yards pick against the real final ESPN
    box score and computes what a real bet of this size would have made, same
    staking/fee math resolve_moneyline_paper_trades uses. Never guesses
    -- a game that isn't final yet, or a player ESPN's box score doesn't
    have a passing-yards number for, is left pending rather than marked
    either way.
    """
    paper_data = load_paper_trades()
    changed = False
    for pick in paper_data.get("passing_yards", []):
        if pick["status"] != "pending" or not pick.get("event_id"):
            continue
        try:
            value, found = context_data.get_player_boxscore_stat(
                pick["league"].lower(), pick["event_id"], pick["player"], "passing_yards"
            )
        except Exception as e:
            print(f"[paper_trading] passing-yards grading error for {pick['player']}: {e}")
            continue
        if not found:
            continue

        won = (value > pick["line"]) if pick["side"] == "over" else (value < pick["line"])
        pick["status"] = "won" if won else "lost"
        pick["resolved_at"] = datetime.now().isoformat()
        pick["final_value"] = value

        price = pick["entry_price"]
        contracts = max(1.0, PAPER_STAKE_DOLLARS / price)
        pick["stake_dollars"] = round(price * contracts, 4)
        entry_fee = _kalshi_taker_fee_dollars_local(price, contracts)
        pick["hypothetical_pnl"] = round(((1.0 - price) * contracts if won else -price * contracts) - entry_fee, 4)
        changed = True

        balance, is_down, down_by = record_paper_bankroll_change(
            "passing_yards", pick["hypothetical_pnl"], f"{pick['player']}-{pick['line']}",
            note=f"{pick['league']} {pick['player']} {pick['side']} {pick['line']} {pick['status']}",
        )

        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}"
            bankroll_str = f" | paper bankroll: ${balance:.2f}" + (f" (down ${down_by:.2f})" if is_down else "")
            send_discord_fn(
                webhook,
                f"[RESOLVED - PASSING YDS] {pick['player']} {pick['side'].upper()} {pick['line']}: "
                f"{pick['status'].upper()} (actual {value:.0f} yds, hypothetical P&L: {pnl_str}{bankroll_str})"
            )

    if changed:
        save_paper_trades(paper_data)


# ---------------------------------------------------------------------------
# Profitability milestone alerts (NEW 2026-09-10, at the user's request)
#
# Every paper pick across every category is already tracked with full
# outcome data (win/loss, hypothetical P&L) -- this doesn't add new
# tracking, it watches the tracking that already exists and speaks up
# the moment a category has BOTH a real sample size AND genuine profit,
# so the user knows when there's an actual case for turning real money
# on for that strategy -- instead of having to keep checking manually.
#
# IMPORTANT: this only ALERTS. It never turns real trading on by itself
# -- that's a real-money decision that needs the user's explicit
# go-ahead every time, not something this bot decides on its own.
# ---------------------------------------------------------------------------
PROFITABILITY_ALERTS_FILE = os.path.join(DATA_DIR, "profitability_alerts.json")


# Categories that could ever become a REAL trade (moneyline picks real
# sports games). Parlay and props can NEVER place a real trade (Kalshi
# has no parlay or player-prop product) -- they get a one-time
# informational note instead of a repeating "turn it on" nag, since
# there's no "on" switch for them.
PROFITABILITY_REAL_CAPABLE_CATEGORIES = {"moneyline"}

# How often to re-remind about a still-profitable, still-not-turned-on
# category -- daily, not every ~1-2 minute cycle, at the user's request
# to "keep letting me know till I see it" without being pure noise.
PROFITABILITY_REALERT_HOURS = 20


def check_profitability_milestones(send_discord_fn=None, webhook=None, real_trading_on_by_category=None):
    """
    Runs once per cycle. For each category (moneyline/parlay/props),
    checks whether it has crossed BOTH a real sample size
    (MIN_SAMPLE_FOR_ADJUSTMENT resolved picks) and genuine profit
    (total_hypothetical_pnl > 0).

    moneyline (real_trading_on_by_category tells us if real trading
    is already on for each): keeps re-alerting once a day, at the user's
    explicit request ("keep letting me know till I see it, so I can fund
    the account and turn it on"), until real trading is actually turned
    on for that one -- then it stops, since the point's been made.

    parlay/props: can never place a real trade at all (no real product
    exists on Kalshi for either), so this sends ONE informational note
    instead of a repeating "turn it on" reminder that would never
    resolve.

    Never raises.
    """
    try:
        real_trading_on_by_category = real_trading_on_by_category or {}
        state = safe_read_json(PROFITABILITY_ALERTS_FILE, {})
        summary = get_paper_trade_summary()
        changed = False
        now = datetime.now()

        for category, stats in summary.items():
            resolved = stats.get("resolved", 0)
            pnl = stats.get("total_hypothetical_pnl")
            if resolved < MIN_SAMPLE_FOR_ADJUSTMENT or pnl is None or pnl <= 0:
                continue

            win_rate = stats.get("win_rate") or 0.0
            entry = state.setdefault(category, {})

            if category in PROFITABILITY_REAL_CAPABLE_CATEGORIES:
                if real_trading_on_by_category.get(category):
                    continue  # already funded/turned on -- point made, stop nagging
                last_at = entry.get("last_alert_at")
                if last_at:
                    try:
                        hours_since = (now - datetime.fromisoformat(last_at)).total_seconds() / 3600
                        if hours_since < PROFITABILITY_REALERT_HOURS:
                            continue
                    except Exception:
                        pass
                entry["last_alert_at"] = now.isoformat()
                entry["resolved"], entry["pnl"] = resolved, pnl
                changed = True
                if send_discord_fn and webhook:
                    send_discord_fn(
                        webhook,
                        f"[STILL PROFITABLE] {category.upper()}: {resolved} resolved picks, ${pnl:+.2f} real "
                        f"hypothetical profit ({win_rate:.1f}% correct), and real trading is STILL OFF for this. "
                        f"Reminding you daily until you fund the account and turn it on, like you asked -- "
                        f"I won't flip it on myself."
                    )
            else:
                if entry.get("informed"):
                    continue
                entry["informed"] = True
                entry["resolved"], entry["pnl"] = resolved, pnl
                changed = True
                if send_discord_fn and webhook:
                    send_discord_fn(
                        webhook,
                        f"[PROFITABLE, PAPER-ONLY FOREVER] {category.upper()}: {resolved} resolved picks, ${pnl:+.2f} "
                        f"hypothetical profit ({win_rate:.1f}% correct) -- good data, but this one can never place a "
                        f"real trade (Kalshi has no product for it), so there's no 'turn it on' step here, just FYI."
                    )

        if changed:
            atomic_write_json(PROFITABILITY_ALERTS_FILE, state)
    except Exception as e:
        print(f"[paper_trading] check_profitability_milestones error: {e}")


# ---------------------------------------------------------------------------
# Manual bet tracking -- lets the user log a real bet THEY placed themselves
# (on Kalshi, with their own money) against a specific pick the bot already
# made, so we can build real data on how their own manual betting performs,
# and specifically whether following the bot's higher-tier ("strong")
# recommendations actually does better than picks they chose on their own
# from the "thin edge" tier or otherwise. This never places anything or
# touches real trading -- it's a log the user fills in after the fact.
# ---------------------------------------------------------------------------
def _find_pick_by_id(paper_data, pick_id):
    """Looks up a moneyline or passing_yards pick by its pick_id. Returns
    (category, pick_dict) or (None, None) if not found (e.g. an older pick
    made before pick_id existed)."""
    for p in paper_data.get("moneyline", []):
        if p.get("pick_id") == pick_id:
            return "moneyline", p
    for p in paper_data.get("passing_yards", []):
        if p.get("pick_id") == pick_id:
            return "passing_yards", p
    return None, None


def record_manual_bet(pick_id, stake_dollars, side_note=None):
    """
    Logs that the user placed a real bet of stake_dollars on the pick
    identified by pick_id (a moneyline or passing_yards pick_id). Snapshots
    the pick's bet_tier and label at the time of logging so later changes
    to thresholds don't retroactively change what was recommended when the
    bet was made. Never raises -- returns the logged entry, or None if the
    pick_id wasn't found.
    """
    try:
        paper_data = load_paper_trades()
        category, pick = _find_pick_by_id(paper_data, pick_id)
        if pick is None:
            return None

        if category == "moneyline":
            label = f"{pick['picked_team']} {pick['league']}"
        else:
            label = f"{pick['player']} {pick['side'].upper()} {pick['line']} yds ({pick['league']})"

        entry = {
            "manual_bet_id": uuid.uuid4().hex[:12],
            "pick_id": pick_id,
            "category": category,
            "label": label,
            "bet_tier_at_bet_time": pick.get("bet_tier"),
            "followed_recommendation": pick.get("bet_tier") == "strong",
            "stake_dollars": round(float(stake_dollars), 2),
            "logged_at": datetime.now().isoformat(),
            "note": side_note,
        }
        paper_data.setdefault("manual_bets", []).append(entry)
        save_paper_trades(paper_data)
        return entry
    except Exception as e:
        print(f"[paper_trading] record_manual_bet error: {e}")
        return None


def get_manual_bets_with_status():
    """
    Returns every logged manual bet joined with its underlying pick's
    current status/result, newest first, so the dashboard can show real
    outcomes as they resolve. Each entry gets a "result_pnl" -- the user's
    own stake scaled by the same $/contract return the paper pick tracked
    -- and "status" (won/lost/pending/unknown if the source pick vanished).
    """
    try:
        paper_data = load_paper_trades()
        out = []
        for entry in paper_data.get("manual_bets", []):
            category, pick = _find_pick_by_id(paper_data, entry.get("pick_id"))
            row = dict(entry)
            if pick is None:
                row["status"] = "unknown"
                row["result_pnl"] = None
            else:
                row["status"] = pick.get("status", "pending")
                pick_pnl = pick.get("hypothetical_pnl")
                pick_stake = pick.get("stake_dollars") or PAPER_STAKE_DOLLARS
                if pick_pnl is not None and pick_stake:
                    # scale the pick's own $ result to the user's actual stake
                    row["result_pnl"] = round(pick_pnl * (entry["stake_dollars"] / pick_stake), 2)
                else:
                    row["result_pnl"] = None
            out.append(row)
        out.sort(key=lambda r: r.get("logged_at") or "", reverse=True)
        return out
    except Exception as e:
        print(f"[paper_trading] get_manual_bets_with_status error: {e}")
        return []


def get_manual_bet_summary():
    """
    Aggregate stats across every logged manual bet: total logged, resolved
    count/win rate/$ pnl overall, and the same split out for bets that
    followed a "strong" (bot-recommended) pick vs everything else -- so it's
    possible to actually see whether following the bot's top tier beats
    picks the user chose on their own from the wider pool. Never raises.
    """
    try:
        rows = get_manual_bets_with_status()

        def _summarize(subset):
            resolved = [r for r in subset if r["status"] in ("won", "lost")]
            wins = [r for r in resolved if r["status"] == "won"]
            pnl = sum(r["result_pnl"] or 0.0 for r in resolved)
            return {
                "logged": len(subset),
                "resolved": len(resolved),
                "win_rate": round(100 * len(wins) / len(resolved), 1) if resolved else None,
                "pnl": round(pnl, 2),
            }

        followed = [r for r in rows if r.get("followed_recommendation")]
        own_pick = [r for r in rows if not r.get("followed_recommendation")]
        return {
            "overall": _summarize(rows),
            "followed_recommendation": _summarize(followed),
            "own_choice": _summarize(own_pick),
        }
    except Exception as e:
        print(f"[paper_trading] get_manual_bet_summary error: {e}")
        return {"overall": {"logged": 0, "resolved": 0, "win_rate": None, "pnl": 0.0},
                "followed_recommendation": {"logged": 0, "resolved": 0, "win_rate": None, "pnl": 0.0},
                "own_choice": {"logged": 0, "resolved": 0, "win_rate": None, "pnl": 0.0}}


def get_loggable_picks(limit=40):
    """
    Recent + pending moneyline and passing_yards picks, newest first, for
    the dashboard's "log a bet you placed" dropdown -- includes each pick's
    bet_tier so the user can see the label right in the picker.
    """
    try:
        paper_data = load_paper_trades()
        rows = []
        for p in paper_data.get("moneyline", []):
            if not p.get("pick_id"):
                continue
            rows.append({
                "pick_id": p["pick_id"],
                "label": f"{p['picked_team']} ({p['league']}) vs edge {p.get('edge_pct') or 0:.1f}%",
                "bet_tier": p.get("bet_tier"),
                "picked_at": p.get("picked_at"),
            })
        for p in paper_data.get("passing_yards", []):
            if not p.get("pick_id"):
                continue
            rows.append({
                "pick_id": p["pick_id"],
                "label": f"{p['player']} {p['side'].upper()} {p['line']} yds ({p['league']})",
                "bet_tier": p.get("bet_tier"),
                "picked_at": p.get("picked_at"),
            })
        rows.sort(key=lambda r: r.get("picked_at") or "", reverse=True)
        return rows[:limit]
    except Exception as e:
        print(f"[paper_trading] get_loggable_picks error: {e}")
        return []


# ---------------------------------------------------------------------------
# WNBA combined-stat picks (points+rebounds+assists and other multi-stat
# combos -- what PrizePicks/sportsbooks usually call "PRA" or a "combo"
# prop), added at the user's request as a second high-volume, independently
# graded main-focus category alongside passing yards. SharpAPI's exact
# stat_category string for these wasn't independently verified live before
# this was written (same caveat as elsewhere in this file), so instead of
# hardcoding one exact name, _is_combined_stat_type below recognizes ANY
# stat_type that names at least two of points/rebounds/assists -- covers
# "points_rebounds_assists", "pts_reb_ast", "pra", "points_rebounds", etc.
# regardless of the exact string SharpAPI actually sends. If a WNBA prop
# feed never sends anything like that, this just never fires -- same
# safe-fail behavior as every other unverified field in this codebase.
# ---------------------------------------------------------------------------
WNBA_COMBINED_STATS_LEAGUES = {"wnba"}
WNBA_COMBINED_STAT_MIN_PROB = float(os.getenv("WNBA_COMBINED_STAT_MIN_PROB", "0.52"))

# Every abbreviation/spelling for each component stat that a sportsbook
# feed might use, checked token-by-token (not a raw substring search --
# "ast" as a token means assists, but "ast" glued inside a longer word
# shouldn't false-positive). "pra"/"pr"/"pa"/"ra" are fused 2-3-letter
# tokens some feeds use for the whole combo in one word.
_STAT_TOKEN_MAP = {
    "points": {"points", "point", "pts", "pt"},
    "rebounds": {"rebounds", "rebound", "reb", "rebs"},
    "assists": {"assists", "assist", "ast", "asts"},
}
_FUSED_TOKEN_MAP = {
    "pra": ["points", "rebounds", "assists"],
    "pr": ["points", "rebounds"],
    "rp": ["points", "rebounds"],
    "pa": ["points", "assists"],
    "ap": ["points", "assists"],
    "ra": ["rebounds", "assists"],
    "ar": ["rebounds", "assists"],
}


def _combined_stat_components(stat_type):
    """
    Returns the list of component ESPN box-score stat keys (e.g.
    ["points", "rebounds", "assists"]) a stat_type string represents, IF
    it names two or more of points/rebounds/assists -- checked token by
    token (splitting on _, +, -, space) against every spelling/abbreviation
    a feed might use ("points_rebounds_assists", "pts_reb_ast", a fused
    "pra" token, etc.), not a raw substring search, so short abbreviations
    like "pts" or "ast" can't false-positive inside an unrelated word.
    Returns [] if it's not a combined stat (e.g. plain "points", or an
    unrelated stat like "steals").
    """
    import re
    s = (stat_type or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t]

    found_order = []
    for token in tokens:
        for full, spellings in _STAT_TOKEN_MAP.items():
            if token in spellings and full not in found_order:
                found_order.append(full)
                break
        else:
            if token in _FUSED_TOKEN_MAP:
                for full in _FUSED_TOKEN_MAP[token]:
                    if full not in found_order:
                        found_order.append(full)

    return found_order if len(found_order) >= 2 else []


def maybe_make_wnba_combined_picks(league, prop_rows, send_discord_fn=None, webhook=None):
    """
    Mirrors maybe_make_passing_yards_picks exactly, but for WNBA combined
    (multi-stat) props instead of QB passing yards -- runs once per cycle,
    fed that cycle's raw SharpAPI player-prop rows, makes ONE independent
    paper pick per (player, line) where consensus clears
    WNBA_COMBINED_STAT_MIN_PROB, no all-or-nothing bundling. Never raises.
    """
    if league not in WNBA_COMBINED_STATS_LEAGUES:
        return []
    try:
        parsed = [
            p for p in (_parse_player_prop_row(r) for r in prop_rows)
            if p and _combined_stat_components(p["stat_type"])
        ]
        if not parsed:
            return []

        groups = {}
        for p in parsed:
            key = (p["player"], p["stat_type"], p["line"], p["side"])
            groups.setdefault(key, []).append(p)

        paper_data = load_paper_trades()
        # Dedup on (player, stat_type, event_id) -- NOT line, same
        # reasoning as passing_yards above (line drift shouldn't create a
        # new pick), but stat_type IS kept here since one player can have
        # multiple distinct combined-stat props (PR, PA, RA, PRA) live on
        # the same game at once and those are genuinely different bets.
        already_picked = {
            (p["player"], p["stat_type"], p.get("event_id"))
            for p in paper_data["wnba_combined"] if p["status"] == "pending"
        }

        # Collapse across every LINE offered for a given (player, stat_type)
        # -- main line and any alternate/"goblin" lines alike -- and keep
        # only the safest one (highest consensus probability). stat_type
        # stays a separate key from line/side (points+rebounds and
        # points+assists for the same player are genuinely different
        # bets), but within one stat_type there's no reason to take a
        # marginal ~52-53% main-line coinflip when a safer alt line is
        # also on offer for the same player/stat.
        by_player_line = {}
        for (player, stat_type, line, side), rows in groups.items():
            probs = [r["prob"] for r in rows if r["prob"] is not None]
            if not probs:
                continue
            avg_prob = sum(probs) / len(probs)
            pl_key = (player, stat_type)
            existing = by_player_line.get(pl_key)
            if existing is None or avg_prob > existing["consensus_prob"]:
                sample = rows[0]
                by_player_line[pl_key] = {
                    "player": player, "stat_type": stat_type, "line": line, "side": side,
                    "consensus_prob": round(avg_prob, 4), "book_count": len(probs),
                    "event_id": sample.get("event_id"), "away_team": sample.get("away_team"),
                    "home_team": sample.get("home_team"),
                    "is_alternate_line": any(r.get("is_alternate_line") for r in rows),
                }

        new_picks = []
        for (player, stat_type), cand in by_player_line.items():
            line = cand["line"]
            if cand["consensus_prob"] < WNBA_COMBINED_STAT_MIN_PROB:
                continue
            event_id = cand.get("event_id") or context_data.get_event_id_for_matchup(
                league, cand.get("away_team"), cand.get("home_team")
            )
            dedup_key = (player, cand["stat_type"], event_id)
            if dedup_key in already_picked:
                continue

            pick = {
                "pick_id": uuid.uuid4().hex[:12],
                "player": player,
                "league": league.upper(),
                "stat_type": cand["stat_type"],
                "line": line,
                "side": cand["side"],
                "consensus_prob": cand["consensus_prob"],
                "book_count": cand["book_count"],
                "is_alternate_line": cand.get("is_alternate_line", False),
                "event_id": event_id,
                "away_team": cand.get("away_team"),
                "home_team": cand.get("home_team"),
                "entry_price": cand["consensus_prob"],
                "picked_at": datetime.now().isoformat(),
                "status": "pending",
                "bet_tier": "strong" if cand["consensus_prob"] >= 0.60 else "thin",
                "pick_score": round(max(0.0, min(1.0, (cand["consensus_prob"] - 0.50) / 0.20)) * 100),
            }
            paper_data["wnba_combined"].append(pick)
            new_picks.append(pick)
            already_picked.add(dedup_key)

        if new_picks:
            save_paper_trades(paper_data)
            if send_discord_fn and webhook:
                lines = [f"[PAPER WNBA COMBINED STATS] {len(new_picks)} pick(s) this cycle:"]
                for p in new_picks:
                    lines.append(f"- {p['player']} {p['side'].upper()} {p['line']} {p['stat_type']} ({p['consensus_prob']*100:.0f}%)")
                send_discord_fn(webhook, "\n".join(lines))

        return new_picks
    except Exception as e:
        print(f"[paper_trading] maybe_make_wnba_combined_picks error: {e}")
        return []


def resolve_wnba_combined_picks(send_discord_fn=None, webhook=None):
    """
    Checks each pending WNBA combined-stat pick against the real final
    ESPN box score. Since ESPN only reports individual stat lines (points,
    rebounds, assists), this sums each component stat for the combo --
    e.g. "points_rebounds_assists" sums PTS+REB+AST. If ANY component
    can't be found (game not final, player missing from that stat group),
    the whole pick stays pending rather than resolving off a partial sum.
    """
    paper_data = load_paper_trades()
    changed = False
    for pick in paper_data.get("wnba_combined", []):
        if pick["status"] != "pending" or not pick.get("event_id"):
            continue
        components = _combined_stat_components(pick["stat_type"])
        if not components:
            continue
        try:
            total = 0.0
            all_found = True
            for component in components:
                value, found = context_data.get_player_boxscore_stat(
                    pick["league"].lower(), pick["event_id"], pick["player"], component
                )
                if not found:
                    all_found = False
                    break
                total += value
        except Exception as e:
            print(f"[paper_trading] WNBA combined-stat grading error for {pick['player']}: {e}")
            continue
        if not all_found:
            continue

        won = (total > pick["line"]) if pick["side"] == "over" else (total < pick["line"])
        pick["status"] = "won" if won else "lost"
        pick["resolved_at"] = datetime.now().isoformat()
        pick["final_value"] = total

        price = pick["entry_price"]
        contracts = max(1.0, PAPER_STAKE_DOLLARS / price)
        pick["stake_dollars"] = round(price * contracts, 4)
        entry_fee = _kalshi_taker_fee_dollars_local(price, contracts)
        pick["hypothetical_pnl"] = round(((1.0 - price) * contracts if won else -price * contracts) - entry_fee, 4)
        changed = True

        balance, is_down, down_by = record_paper_bankroll_change(
            "wnba_combined", pick["hypothetical_pnl"], f"{pick['player']}-{pick['line']}",
            note=f"{pick['league']} {pick['player']} {pick['side']} {pick['line']} {pick['status']}",
        )

        if send_discord_fn and webhook:
            pnl_str = f"${pick['hypothetical_pnl']:+.2f}"
            bankroll_str = f" | paper bankroll: ${balance:.2f}" + (f" (down ${down_by:.2f})" if is_down else "")
            send_discord_fn(
                webhook,
                f"[RESOLVED - WNBA COMBINED] {pick['player']} {pick['side'].upper()} {pick['line']} {pick['stat_type']}: "
                f"{pick['status'].upper()} (actual {total:.0f}, hypothetical P&L: {pnl_str}{bankroll_str})"
            )

    if changed:
        save_paper_trades(paper_data)
