import os
from flask import Flask, render_template_string, request, redirect
from pykalshi import KalshiClient

import ledger
from state_io import safe_read_json

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Design intent: minimalist but functional. One glance should answer three
# questions -- is real money at risk right now, is the paper simulation
# winning or losing, and how close is it to having enough data to trust.
# Everything else (raw trade logs, budget math) is real and correct but
# secondary, so it's tucked into <details> instead of competing for
# attention with the numbers that actually matter day to day.
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AR894 Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { background:#0d1117; color:#e6edf3; font-family:-apple-system,sans-serif; padding:16px; margin:0; max-width:640px; margin:0 auto; }
  h1 { font-size:1.2em; margin:0 0 12px 0; }
  h3 { font-size:0.75em; text-transform:uppercase; letter-spacing:0.04em; color:#8b949e; margin:0 0 10px 0; }
  .status { display:flex; align-items:center; gap:8px; padding:10px 14px; border-radius:10px; font-weight:600; font-size:0.9em; margin-bottom:16px; }
  .status.paused { background:#3b2205; color:#e3b341; border:1px solid #9e6a03; }
  .status.live { background:#0d2818; color:#3fb950; border:1px solid #238636; }
  .dot { width:8px; height:8px; border-radius:50%; background:currentColor; flex-shrink:0; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:16px; margin-bottom:12px; }
  .row { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; font-size:0.9em; }
  .row:last-child { margin-bottom:0; }
  .row .label { color:#8b949e; }
  .big { font-size:1.9em; font-weight:700; line-height:1.1; }
  .green { color:#3fb950; }
  .red { color:#f85149; }
  .muted { color:#8b949e; font-size:0.82em; }
  .sub { color:#8b949e; font-size:0.8em; margin-top:2px; }
  .badge { font-size:0.72em; padding:2px 8px; border-radius:10px; background:#21262d; color:#8b949e; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width:480px) { .grid2 { grid-template-columns:1fr; } }
  .bar { background:#21262d; border-radius:6px; height:6px; overflow:hidden; margin-top:8px; }
  .bar-fill { height:100%; border-radius:6px; background:#58a6ff; }
  .alert { background:#3b2205; border:1px solid #9e6a03; border-radius:10px; padding:14px; margin-bottom:16px; }
  table { width:100%; border-collapse:collapse; font-size:0.82em; }
  td { padding:6px 4px; border-bottom:1px solid #21262d; }
  input[type=number] { width:100%; padding:10px; border-radius:8px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; font-size:1em; margin:8px 0; box-sizing:border-box; }
  button { width:100%; padding:12px; border-radius:8px; border:none; background:#238636; color:white; font-size:1em; font-weight:600; }
  details { margin-top:16px; }
  summary { cursor:pointer; color:#8b949e; font-size:0.82em; padding:8px 0; }
  summary:hover { color:#e6edf3; }
</style>
</head>
<body>
  <h1>AR894</h1>

  <div class="status {{ 'live' if real_trading_on else 'paused' }}">
    <span class="dot"></span>
    {% if real_trading_on %}Using real money on: {{ real_trading_summary }}{% else %}Practice mode — no real money is being risked right now{% endif %}
  </div>
  <div class="muted" style="margin:-10px 0 16px 0;">{% if real_trading_on %}The bot is placing real bets with real dollars on the leagues listed above.{% else %}Everything below is a simulation — it tracks what WOULD happen so the strategy can be tested safely before any real money is used.{% endif %}</div>

  {% if pending_deposit %}
  <div class="alert">
    <strong>New deposit detected: ${{ "%.2f"|format(pending_deposit) }}</strong>
    <p class="muted">How much of this am I allowed to use for trading?</p>
    <form method="POST" action="/allocate">
      <input type="number" name="amount" step="0.01" min="0" max="{{ pending_deposit }}" value="{{ pending_deposit }}" required>
      <button type="submit">Set Allowance</button>
    </form>
  </div>
  {% endif %}

  <div class="grid2">
    {% for c in categories %}
    <div class="card">
      <h3>{{ c.label }} — practice bets of ${{ "%.0f"|format(paper_stake) }} each</h3>
      <div class="big {{ 'green' if not c.bankroll_down else 'red' }}">
        ${{ "%.2f"|format(c.bankroll_balance) }}
      </div>
      <div class="sub">{% if c.bankroll_down %}Started with ${{ "%.0f"|format(paper_starting_bankroll) }} play money — currently DOWN ${{ "%.2f"|format(c.bankroll_down_by) }}{% else %}Started with ${{ "%.0f"|format(paper_starting_bankroll) }} play money — currently UP{% endif %}</div>
      <div class="sub" style="margin-top:2px;">↑ Would this strategy be making or losing money, if it were real?</div>

      <div class="row" style="margin-top:14px;">
        <span class="label">How often it's right</span>
        <span>{% if c.summary.resolved > 0 %}{{ "%.0f"|format(c.summary.win_rate) }}% correct (out of {{ c.summary.resolved }} finished bets){% else %}no finished bets yet{% endif %}</span>
      </div>
      {% for s in c.settings %}
      <div class="row">
        <span class="label">{{ s.label }}</span>
        <span>{{ s.value }}</span>
      </div>
      <div class="sub">↑ {{ s.explanation }}</div>
      {% endfor %}

      <div class="row" style="margin-top:12px;">
        <span class="label">Data collected</span>
        <span class="muted">{{ c.sample }} of {{ min_sample }} needed</span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:{{ (100 * c.sample / min_sample)|round(0, 'floor')|int if c.sample < min_sample else 100 }}%;"></div></div>
      <div class="sub">↑ It won't change its own settings until it has {{ min_sample }} finished bets to learn from — right now it's just gathering evidence.</div>
    </div>
    {% endfor %}
  </div>

  <div class="card">
    <h3>Close Calls — Extra Info</h3>
    <div class="sub" style="margin-bottom:10px;">These are picks that only barely qualified as a "safe enough" bet — basically a coin flip with a slight edge. For those, it checks injuries and weather (free, real data) so there's more to go on than just the odds.</div>
    {% if too_close_picks %}
      {% for p in too_close_picks[:6] %}
      <div class="row" style="align-items:flex-start; margin-bottom:10px; border-bottom:1px solid #21262d; padding-bottom:10px;">
        <div>
          <div><strong>{{ p.picked_team }}</strong> <span class="badge">{{ p.league }}</span></div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · {{ "%.0f"|format(p.market_probability*100) }}% likely to win · {{ p.status }}</div>
          {% if p.context_note %}
            <div class="sub" style="white-space:pre-line; margin-top:4px;">{{ p.context_note }}</div>
          {% else %}
            <div class="sub" style="margin-top:4px;">Nothing notable found — no key injuries, normal weather.</div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">None right now — no picks have been this close to a coin flip yet.</div>
    {% endif %}
  </div>

  {% if adaptive.last_adjusted %}
  <div class="muted" style="text-align:center; margin-bottom:8px;">Last time it changed a setting on its own: {{ adaptive.last_adjusted }}</div>
  {% endif %}

  <details>
    <summary>Your real Kalshi account (actual dollars — for reference)</summary>

    <div class="card">
      <div class="row"><span class="label">Money in your Kalshi account</span><span>${{ "%.2f"|format(balance) }}</span></div>
      <div class="row"><span class="label">How much the bot is allowed to use</span><span>${{ "%.2f"|format(available_budget) }}</span></div>
      <div class="row"><span class="label">Tied up in open real bets right now</span><span>${{ "%.2f"|format(committed) }}</span></div>
      <div class="row"><span class="label">Real profit/loss so far (this bot only)</span>
        <span class="{{ 'green' if realized_profit >= 0 else 'red' }}">${{ "%.2f"|format(realized_profit) }}</span>
      </div>
      {% if trading_halted %}
      <div class="row"><span class="label">Real trading status</span>
        <span class="red">HALTED -- {{ halted_reason }}</span>
      </div>
      <div class="muted">Auto-stopped by the loss-limit safety net (halts at {{ "%.0f"|format(loss_limit_percent) }}% of your allowance lost -- never on giving back profit). Paper trading is unaffected. Won't resume on its own -- POST /resume_trading when you're ready.</div>
      {% else %}
      <div class="row"><span class="label">Real trading status</span><span class="green">Active (halts on real losses reaching {{ "%.0f"|format(loss_limit_percent) }}% of allowance{{ " -- currently %.1f%%"|format(current_loss_pct) if current_loss_pct > 0 else "" }})</span></div>
      {% endif %}
    </div>

    <div class="card">
      <h3>Currently Open Real Bets ({{ positions|length }})</h3>
      {% if positions %}
        {% for p in positions %}
        <div class="row"><span>{{ p.ticker }}</span><span class="badge">{{ p.position_fp }} contracts</span></div>
        {% endfor %}
      {% else %}
        <div class="muted">None right now.</div>
      {% endif %}
    </div>

    <div class="card">
      <h3>Recent Real Bets Placed</h3>
      {% if trade_log %}
        <table>
          <tr><td><b>League</b></td><td><b>Matchup</b></td><td><b>Side</b></td><td><b>Edge</b></td><td><b>Stake</b></td></tr>
          {% for t in trade_log[-10:]|reverse %}
          <tr>
            <td>{{ t.league }}</td>
            <td>{{ t.matchup }}</td>
            <td>{{ t.get('side', 'YES') }}</td>
            <td>{{ "%.1f"|format(t.edge_pct) }}%</td>
            <td>${{ "%.2f"|format(t.stake) }}</td>
          </tr>
          {% endfor %}
        </table>
      {% else %}
        <div class="muted">None logged yet.</div>
      {% endif %}
    </div>
  </details>

  <div class="muted" style="margin-top:20px; text-align:center;">AR894 Engine</div>
</body>
</html>
"""


def load_json(filename, default):
    return safe_read_json(os.path.join(DATA_DIR, filename), default)


def get_client():
    os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
    os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])
    return KalshiClient()


@app.route("/")
def dashboard():
    import paper_trading as pt
    import worker

    client = get_client()

    try:
        balance = client.portfolio.get_balance().balance / 100.0
    except Exception:
        balance = 0.0

    try:
        raw_positions = client.portfolio.get_positions()
        positions = [p for p in raw_positions if float(getattr(p, "position_fp", 0) or 0) != 0]
    except Exception:
        positions = []

    led = ledger.load_ledger()
    open_positions_dict = load_json("open_positions.json", {})
    available_budget = ledger.get_available_budget(client, open_positions_dict)
    live_tickers = {p.ticker for p in positions}
    committed = ledger.get_open_position_cost_basis(open_positions_dict, live_tickers)
    # Bot's own P&L only -- NOT Kalshi's account-wide realized_pnl, which
    # also includes any manual trading on this account.
    realized_profit = ledger.get_bot_realized_profit()

    trade_log = load_json("trade_audit_log.json", [])
    summary = pt.get_paper_trade_summary()
    bankroll = pt.load_paper_bankroll()
    ml_bank = bankroll.get("moneyline", {"balance": pt.PAPER_STARTING_BANKROLL})
    btc_bank = bankroll.get("btc", {"balance": pt.PAPER_STARTING_BANKROLL})
    adaptive = pt.load_adaptive_settings()
    min_sample = pt.MIN_SAMPLE_FOR_ADJUSTMENT

    favorite_current = pt.get_effective_favorite_min_prob()
    favorite_default = pt.MONEYLINE_FAVORITE_MIN_PROB_DEFAULT
    favorite_note = f"{favorite_current*100:.0f}%" + (" (raised)" if favorite_current > favorite_default else "")

    btc_window = adaptive.get("btc_momentum_window", pt.BTC_MOMENTUM_WINDOW_DEFAULT)

    btc_fair_prob = pt.get_btc_fair_prob_estimate()
    btc_edge_note = (
        f"Only bets when it estimates at least a {pt.BTC_MIN_EDGE_PCT:.0f}% real edge over the price -- "
        f"added because it was winning most bets but still losing money paying prices that didn't leave "
        f"enough room for profit."
        if btc_fair_prob is not None else
        f"Will start requiring a {pt.BTC_MIN_EDGE_PCT:.0f}% edge once it has {min_sample} finished bets to judge from -- still gathering data for now."
    )

    categories = [
        {
            "key": "moneyline", "label": "Sports Moneyline (NFL/NCAAF/MLB/UFC/ATP/WNBA)",
            "summary": summary["moneyline"],
            "bankroll_balance": ml_bank["balance"],
            "bankroll_down": ml_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - ml_bank["balance"]),
            "settings": [
                {
                    "label": "How sure it must be to bet",
                    "value": favorite_note,
                    "explanation": "It only bets on a team if it thinks they're at least this likely to win. Higher = more cautious, fewer bets.",
                },
            ],
            "sample": min(adaptive.get("moneyline_sample_size", 0), min_sample),
        },
        {
            "key": "btc", "label": "Bitcoin Price (15-min bets)",
            "summary": summary["btc"],
            "bankroll_balance": btc_bank["balance"],
            "bankroll_down": btc_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - btc_bank["balance"]),
            "settings": [
                {
                    "label": "How far back it looks",
                    "value": f"last {btc_window} price checks",
                    "explanation": "It guesses UP or DOWN based on which way the price has moved over this many recent checks.",
                },
                {
                    "label": "Minimum edge required",
                    "value": f"{pt.BTC_MIN_EDGE_PCT:.0f}%" + (f" (est. {btc_fair_prob*100:.0f}% accurate)" if btc_fair_prob is not None else ""),
                    "explanation": btc_edge_note,
                },
            ],
            "sample": min(adaptive.get("btc_sample_size", 0), min_sample),
        },
    ]

    all_moneyline_picks = pt.load_paper_trades().get("moneyline", [])
    too_close_picks = [p for p in all_moneyline_picks if p.get("is_too_close")][-15:][::-1]

    real_trading_on = bool(worker.REAL_TRADING_LEAGUES) or worker.BTC_REAL_TRADING_ENABLED
    real_trading_summary = ", ".join(sorted(worker.REAL_TRADING_LEAGUES)) if worker.REAL_TRADING_LEAGUES else ""
    if worker.BTC_REAL_TRADING_ENABLED:
        real_trading_summary = (real_trading_summary + " + BTC").strip(" +")

    trading_halted = ledger.is_trading_halted()
    bot_pnl_data = ledger.load_bot_pnl()
    current_loss_pct = ledger.get_realized_loss_pct()

    return render_template_string(
        PAGE_TEMPLATE,
        balance=balance,
        positions=positions,
        trade_log=trade_log,
        categories=categories,
        total_allocated=led["total_allocated"],
        committed=committed,
        available_budget=available_budget,
        realized_profit=realized_profit,
        pending_deposit=led.get("pending_deposit_amount"),
        paper_stake=pt.PAPER_STAKE_DOLLARS,
        paper_starting_bankroll=pt.PAPER_STARTING_BANKROLL,
        adaptive=adaptive,
        min_sample=min_sample,
        too_close_picks=too_close_picks,
        real_trading_on=real_trading_on,
        real_trading_summary=real_trading_summary,
        trading_halted=trading_halted,
        halted_reason=bot_pnl_data.get("halted_reason"),
        loss_limit_percent=ledger.MAX_LOSS_PERCENT,
        current_loss_pct=current_loss_pct,
    )


@app.route("/allocate", methods=["POST"])
def allocate():
    amount = float(request.form.get("amount", 0))
    ledger.approve_allocation(amount)
    return redirect("/")


@app.route("/reset_allocation", methods=["POST"])
def reset_allocation():
    """Snap total_allocated back to whatever's actually in the account right
    now -- for correcting drift from manual trading or a past bug, not for
    normal deposit approval (that's still /allocate). No dashboard button
    for this anymore (shouldn't be needed day-to-day) -- available via
    curl/Postman as a manual escape hatch if numbers ever drift again."""
    client = get_client()
    try:
        balance = client.portfolio.get_balance().balance / 100.0
    except Exception:
        return redirect("/")
    led = ledger.load_ledger()
    led["total_allocated"] = balance
    led["pending_deposit_amount"] = None
    led["history"].append({"type": "manual_resync", "amount": balance})
    ledger.save_ledger(led)
    return redirect("/")


@app.route("/trade_audit_log")
def trade_audit_log_route():
    """
    Shows every real-money trade DECISION the bot has logged -- the fair
    probability it calculated, the price Kalshi was offering, and the edge
    it thought it saw -- so a specific trade can be checked after the fact.
    Only logged for sports/moneyline trades (BTC doesn't have a "fair prob"
    to compare against). Newest first. Optional ?league=ufc or ?search=berisha
    to filter.
    """
    log = load_json("trade_audit_log.json", [])
    if not log:
        return "No trade decisions logged yet.\n"
    league = request.args.get("league", "").lower()
    search = request.args.get("search", "").lower()
    if league:
        log = [e for e in log if str(e.get("league", "")).lower() == league]
    if search:
        log = [e for e in log if search in str(e.get("matchup", "")).lower() or search in str(e.get("ticker", "")).lower()]
    log = list(reversed(log))
    return {"count": len(log), "decisions": log}


@app.route("/moneyline_picks")
def moneyline_picks_route():
    """
    Every moneyline PAPER pick the bot has ever made (pending or resolved),
    across every league -- this is the direct answer to "why are there only
    N picks / why isn't it betting on more games." A pick only gets made
    once per Kalshi event ever (see make_moneyline_paper_picks' already_picked
    set), so this list's length IS the total career pick count; it does not
    reset just because the dashboard's "resolved bets" counters are low.
    Optional ?league=nfl or ?status=pending to filter.
    """
    data = load_json("paper_trades.json", {"moneyline": [], "btc": []})
    picks = data.get("moneyline", [])
    league = request.args.get("league", "").lower()
    status = request.args.get("status", "").lower()
    if league:
        picks = [p for p in picks if str(p.get("league", "")).lower() == league]
    if status:
        picks = [p for p in picks if str(p.get("status", "")).lower() == status]
    by_league = {}
    for p in data.get("moneyline", []):
        by_league[p.get("league", "?")] = by_league.get(p.get("league", "?"), 0) + 1
    return {
        "total_career_picks_all_leagues": len(data.get("moneyline", [])),
        "picks_per_league": by_league,
        "filtered_count": len(picks),
        "picks": list(reversed(picks)),
    }


@app.route("/purge_stale_moneyline_picks", methods=["POST"])
def purge_stale_moneyline_picks_route():
    """One-time cleanup for moneyline paper picks made before the
    near-term filter existed. No dashboard button -- trigger with:
    curl -X POST https://<your-app>.up.railway.app/purge_stale_moneyline_picks
    """
    import paper_trading as pt
    client = get_client()
    kept, removed = pt.purge_stale_moneyline_picks(client)
    return f"Kept {kept} near-term picks, removed {removed} stale ones.\n"


@app.route("/live_picks")
def live_picks_route():
    """
    Live/in-game paper picks only (source="live"), plus the raw tracker
    state (every game currently being watched, with its price history)
    so the live strategy's behavior can be checked directly -- e.g.
    confirming it did NOT fire during a price swing that later reversed.
    """
    import paper_trading as pt
    import live_trading
    data = pt.load_paper_trades()
    live_picks = [p for p in data.get("moneyline", []) if p.get("source") == "live"]
    tracker = live_trading._load()
    return {
        "live_trading_enabled": live_trading.LIVE_TRADING_ENABLED,
        "live_picks_count": len(live_picks),
        "live_picks": list(reversed(live_picks)),
        "currently_tracked_games": tracker.get("games", {}),
    }


@app.route("/real_positions")
def real_positions_route():
    """
    Every currently open position on the REAL Kalshi account this bot's
    API key belongs to -- including trades placed manually through
    Kalshi's own site/app, not just ones the bot itself made (it's the
    same account, so it's the same portfolio). Useful for checking "is
    this specific game/market something I hold a position in right now,"
    e.g. to compare a manual trade against what the bot's own filters
    would have decided.
    """
    client = get_client()
    try:
        raw_positions = client.portfolio.get_positions()
    except Exception as e:
        return {"error": f"couldn't fetch positions: {e}"}, 502

    out = []
    for p in raw_positions:
        count = float(getattr(p, "position_fp", 0) or 0)
        if count == 0:
            continue
        ticker = getattr(p, "ticker", None)
        entry = {
            "ticker": ticker,
            "position": count,
            "side": "YES" if count > 0 else "NO",
        }
        try:
            market = client.get_market(ticker)
            entry["market_title"] = getattr(market, "title", None)
            entry["yes_ask"] = getattr(market, "yes_ask_dollars", None)
            entry["no_ask"] = getattr(market, "no_ask_dollars", None)
            entry["status"] = str(getattr(market, "status", None))
        except Exception as e:
            entry["market_lookup_error"] = str(e)
        out.append(entry)

    orders_out = []
    try:
        raw_orders = client.portfolio.get_orders()
        for o in raw_orders:
            status = str(getattr(o, "status", "")).lower()
            if status in ("resting", "pending", "open"):
                orders_out.append({
                    "ticker": getattr(o, "ticker", None),
                    "side": str(getattr(o, "side", None)),
                    "action": str(getattr(o, "action", None)),
                    "status": status,
                    "count": getattr(o, "remaining_count", getattr(o, "count", None)),
                    "price": getattr(o, "yes_price_dollars", getattr(o, "price", None)),
                })
    except Exception as e:
        orders_out = [{"error": f"couldn't fetch orders: {e}"}]

    return {
        "open_positions_count": len(out),
        "positions": out,
        "resting_orders_count": len([o for o in orders_out if "error" not in o]),
        "resting_orders": orders_out,
    }


@app.route("/purge_pre_threshold_picks", methods=["GET", "POST"])
def purge_pre_threshold_picks_route():
    """
    One-time cleanup for a specific, now-understood bug: before commit
    7dde8e9 (2026-09-07) added the `fair_prob >= favorite_min_prob` check,
    make_moneyline_paper_picks only required a 2% edge -- no floor on how
    likely the picked side actually was to win. That let it pick underdogs
    (confirmed live: two ATP picks sitting at ~39-40% win probability,
    nowhere near the 55% bar this bot is supposed to enforce).

    This removes only PENDING picks whose stored market_probability is
    below the CURRENT effective favorite_min_prob -- i.e. picks that could
    never have been made under today's actual rules. Resolved (won/lost)
    picks are always left alone; this doesn't touch real trading history,
    only cleans up paper-trading clutter from before the bug was fixed.
    GET-accessible (not just POST) since it's safe to call more than once
    and safe to trigger from a browser address bar.
    """
    import paper_trading as pt
    threshold = pt.get_effective_favorite_min_prob()
    data = pt.load_paper_trades()
    kept, removed = [], []
    for pick in data["moneyline"]:
        if pick.get("status") == "pending" and pick.get("market_probability", 1.0) < threshold:
            removed.append(pick)
        else:
            kept.append(pick)
    data["moneyline"] = kept
    if removed:
        pt.save_paper_trades(data)
    return {
        "current_threshold": threshold,
        "kept_count": len(kept),
        "removed_count": len(removed),
        "removed_picks": removed,
    }


@app.route("/shard_balance")
def shard_balance_route():
    """
    Diagnostic for Kalshi's new exchange-sharding rollout (crypto markets
    moved to a separate exchange shard on 2026-08-24). Programmatic
    traders must have collateral PRE-ALLOCATED on the shard a market
    lives on before an order can be placed there -- if the account's
    cash is sitting on the default shard (0) and BTC/crypto markets now
    live on shard 2, real BTC orders can fail/reject for balance reasons
    that have nothing to do with the trading logic itself.

    This calls whatever balance/shard-aware methods the installed
    pykalshi client actually exposes and reports back what it finds,
    including a raw introspection of available methods, so this can be
    diagnosed from real account data instead of guessing at the
    library's surface. No dashboard button -- hit directly:
    https://<your-app>.up.railway.app/shard_balance
    """
    client = get_client()
    result = {}

    try:
        result["default_balance_cents"] = client.portfolio.get_balance().balance
    except Exception as e:
        result["default_balance_error"] = str(e)

    portfolio_methods = [m for m in dir(client.portfolio) if not m.startswith("_")]
    result["portfolio_methods_available"] = portfolio_methods

    # Try every plausible shard-aware call the client might expose, without
    # assuming which one (if any) this version of pykalshi actually has.
    per_shard = {}
    for shard_idx in range(4):
        for method_name, kwargs in [
            ("get_balance", {"exchange_index": shard_idx}),
            ("get_balances", {"exchange_index": shard_idx}),
        ]:
            method = getattr(client.portfolio, method_name, None)
            if method is None:
                continue
            try:
                r = method(**kwargs)
                per_shard[f"shard_{shard_idx}_via_{method_name}"] = getattr(r, "balance", r)
            except Exception as e:
                per_shard[f"shard_{shard_idx}_via_{method_name}_error"] = str(e)
    result["per_shard_attempts"] = per_shard

    # Some client versions expose a raw request/session escape hatch --
    # capture what's there so a raw GET /portfolio/balance?exchange_index=
    # can be attempted if the typed methods don't support shards yet.
    raw_attrs = [a for a in dir(client) if not a.startswith("__") and a not in ("portfolio",)]
    result["client_top_level_attrs"] = raw_attrs

    # Per Kalshi's own docs, GET /portfolio/balance (no exchange_index)
    # already aggregates across every shard AND returns a
    # "balance_breakdown" array showing the split per exchange_index --
    # this is the real answer to "is money actually sitting on the
    # crypto shard", independent of whether pykalshi's typed
    # get_balance() wrapper happens to expose that field yet.
    for method_name in ("get", "_request", "paginated_get"):
        method = getattr(client, method_name, None)
        if method is None:
            continue
        try:
            raw = method("/portfolio/balance")
            result[f"raw_balance_via_{method_name}"] = raw
            break
        except Exception as e:
            result[f"raw_balance_via_{method_name}_error"] = str(e)

    return result


@app.route("/fund_crypto_shard", methods=["POST"])
def fund_crypto_shard_route():
    """
    TEST/DIAGNOSTIC ONLY for now -- not wired into the trading loop yet.
    Moves real money from the default exchange shard (0) to the crypto
    shard (2) via Kalshi's Intra Account Transfer endpoint, so this can
    be validated with a small real transfer BEFORE any automatic
    version of this gets built into process_btc_real_trading. POST only
    (never GET) so a browser preview or crawler can't trigger it by
    accident. Trigger with:
    curl -X POST https://<your-app>.up.railway.app/fund_crypto_shard -d amount=1.00

    "amount" is dollars (defaults to 1.00 -- deliberately tiny for the
    first real test). Kalshi's transfer endpoint takes the amount in
    CENTICENTS (1/100 of a cent -- i.e. dollars * 10000), per their own
    API docs, which is a different unit than get_balance()'s cents.
    """
    amount_dollars = float(request.form.get("amount", request.args.get("amount", 1.00)))
    client = get_client()
    result = {"requested_amount_dollars": amount_dollars}

    try:
        before = client.get("/portfolio/balance")
        result["balance_before"] = before.get("balance_breakdown")
    except Exception as e:
        result["balance_before_error"] = str(e)

    body = {
        "source": "event_contract",
        "destination": "event_contract",
        "amount": round(amount_dollars * 10000),
        "source_exchange_shard": 0,
        "destination_exchange_shard": 2,
    }
    result["request_body"] = body

    for method_name, call in [
        ("post_json", lambda: client.post("/portfolio/intra_exchange_instance_transfer", json=body)),
        ("post_data", lambda: client.post("/portfolio/intra_exchange_instance_transfer", data=body)),
        ("post_positional", lambda: client.post("/portfolio/intra_exchange_instance_transfer", body)),
    ]:
        try:
            resp = call()
            result["transfer_response"] = resp
            result["transfer_method_used"] = method_name
            break
        except Exception as e:
            result[f"{method_name}_error"] = str(e)

    return result


@app.route("/btc_price_paths")
def btc_price_paths_route():
    """
    BTC paper trades with their recorded contract price history (see
    paper_trading.track_btc_contract_prices) -- resolved trades show the
    full path from entry to resolution, so a real early-exit percentage
    can be picked from actual data instead of guessed at. Optional
    ?status=won / ?status=lost / ?status=pending to filter; defaults to
    resolved trades that actually have price history recorded (older
    trades won't -- this started fresh, not retroactively).
    """
    import paper_trading as pt
    data = pt.load_paper_trades()
    btc = data.get("btc", [])
    status_filter = request.args.get("status")
    if status_filter:
        btc = [p for p in btc if p.get("status") == status_filter]
    else:
        btc = [p for p in btc if p.get("status") in ("won", "lost") and p.get("contract_price_history")]
    return {
        "count": len(btc),
        "trades": [
            {
                "ticker": p["ticker"], "status": p["status"], "predicted_direction": p["predicted_direction"],
                "entry_price": p.get("entry_price"), "contract_price_history": p.get("contract_price_history", []),
                "picked_at": p.get("picked_at"), "resolved_at": p.get("resolved_at"),
            }
            for p in btc
        ],
    }


@app.route("/debug_selections")
def debug_selections_route():
    """
    Diagnostic for the moneyline funnel's biggest bottleneck across most
    leagues (no_two_sided_odds -- see the funnel counters added to
    make_moneyline_paper_picks). Groups one league's raw SharpAPI rows by
    event_id and shows every distinct "selection" string seen for that
    event -- if the same team shows up spelled differently across
    sportsbooks, this is where it'd show up as 3+ distinct selections
    for a game that should only have 2. ?league=mlb (default) or any
    other league key.
    """
    import worker
    league = request.args.get("league", "mlb")
    rows = worker.fetch_sharpapi_odds(league)
    from collections import defaultdict as dd
    by_event = dd(lambda: dd(set))
    for row in rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        by_event[row.get("event_id")]["selections"].add(row.get("selection"))
        by_event[row.get("event_id")]["books"].add(row.get("sportsbook"))
        by_event[row.get("event_id")]["teams"] = (row.get("away_team"), row.get("home_team"))

    problem_events = {
        eid: {"selections": sorted(v["selections"]), "books": sorted(v["books"]), "teams": v["teams"]}
        for eid, v in by_event.items() if len(v["selections"]) != 2
    }

    # Raw per-row dump for ONE problem event, to see whether away_team/
    # home_team (not just "selection") also vary by sportsbook, or if
    # only "selection" is inconsistent -- decides how the fix has to work.
    raw_rows_for_one_event = []
    if problem_events:
        target_event = next(iter(problem_events))
        for row in rows:
            if row.get("event_id") == target_event and row.get("market_type") == "moneyline":
                raw_rows_for_one_event.append({
                    "sportsbook": row.get("sportsbook"), "selection": row.get("selection"),
                    "away_team": row.get("away_team"), "home_team": row.get("home_team"),
                    "is_main_line": row.get("is_main_line"),
                })

    # Same check using the FIXED grouping (paper_trading._selection_side),
    # which groups by each row's own away/home role instead of the raw
    # selection string -- shows how many events the fix actually resolves.
    import paper_trading as pt
    fixed_by_event = dd(set)
    for row in rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        side = pt._selection_side(row.get("selection"), row.get("away_team"), row.get("home_team"))
        if side is None:
            continue
        fixed_by_event[row.get("event_id")].add(side)
    fixed_problem_events = {eid: sorted(sides) for eid, sides in fixed_by_event.items() if len(sides) != 2}

    return {
        "league": league,
        "total_rows": len(rows),
        "total_distinct_events": len(by_event),
        "events_with_wrong_selection_count": len(problem_events),
        "sample_problem_events": dict(list(problem_events.items())[:10]),
        "raw_rows_for_one_problem_event": raw_rows_for_one_event,
        "FIXED_events_with_wrong_side_count": len(fixed_problem_events),
        "FIXED_sample_problem_events": dict(list(fixed_problem_events.items())[:10]),
    }


@app.route("/run_moneyline_scan")
def run_moneyline_scan_route():
    """
    Runs the REAL moneyline scan-and-pick logic for one league right now
    (not a simulation of it -- this IS what the worker loop calls every
    cycle) and returns the funnel counts plus any new picks made, so we
    can see exactly why a league is or isn't picking without waiting for
    the next 5-minute cycle or digging through Railway logs.
    ?league=mlb (default)
    """
    import worker
    import paper_trading as pt
    import io
    import contextlib

    league = request.args.get("league", "mlb")
    client = get_client()
    rows = worker.fetch_sharpapi_odds(league)
    kalshi_markets = worker.get_open_markets(client, worker.LEAGUE_SERIES[league])
    kalshi_events = worker.group_kalshi_markets_by_event(kalshi_markets)

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        new_picks = pt.make_moneyline_paper_picks(
            league, rows, kalshi_events, worker.safe_match_event, worker.send_discord, worker.DISCORD_WEBHOOK_UPDATES
        )

    printed = captured.getvalue().strip()
    funnel_line = next((l for l in printed.splitlines() if "moneyline funnel" in l), printed)

    return {
        "league": league,
        "funnel": funnel_line,
        "new_picks_this_call": new_picks,
        "kalshi_events_found": len(kalshi_events),
    }


@app.route("/circuit_breaker_status")
def circuit_breaker_status_route():
    """Whether real trading is currently auto-halted by the drawdown
    circuit breaker, and the numbers behind that decision."""
    import ledger
    data = ledger.load_bot_pnl()
    return {
        "halted": data.get("halted", False),
        "halted_reason": data.get("halted_reason"),
        "halted_at": data.get("halted_at"),
        "bot_lifetime_pnl": data.get("total", 0.0),
        "current_realized_loss_pct": ledger.get_realized_loss_pct(),
        "loss_limit_percent": ledger.MAX_LOSS_PERCENT,
        "allocated_budget": ledger.load_ledger().get("total_allocated", 0.0),
    }


@app.route("/resume_trading", methods=["POST"])
def resume_trading_route():
    """Manually clears the drawdown circuit breaker's halt so real trading
    (sports + BTC, whichever you've separately enabled) can resume. This
    never happens automatically -- only this call clears it, so a losing
    streak can't quietly turn itself back on."""
    import ledger
    was_halted = ledger.is_trading_halted()
    ledger.resume_trading()
    return {"was_halted": was_halted, "now_halted": ledger.is_trading_halted()}


@app.route("/debug_kalshi_match")
def debug_kalshi_match_route():
    """
    Shows Kalshi's own market titles for a league's open events side by
    side with today's SharpAPI away/home team names, so a match failure
    (no_kalshi_match in the funnel) can be diagnosed -- is Kalshi's title
    formatted differently than normalize_team_name expects, or is the
    game just not listed on Kalshi at all. ?league=mlb (default).
    """
    import worker
    from datetime import datetime, timezone

    league = request.args.get("league", "mlb")
    client = get_client()
    rows = worker.fetch_sharpapi_odds(league)
    kalshi_markets = worker.get_open_markets(client, worker.LEAGUE_SERIES[league])
    kalshi_events = worker.group_kalshi_markets_by_event(kalshi_markets)

    kalshi_side = {}
    for event_ticker, markets in kalshi_events.items():
        kalshi_side[event_ticker] = [
            {"title": m.title, "normalized": worker.normalize_team_name(worker.short_name(m.title))}
            for m in markets
        ]

    now = datetime.now(timezone.utc)
    sharp_today = {}
    seen_events = set()
    for row in rows:
        if row.get("is_main_line") is not True or row.get("market_type") != "moneyline":
            continue
        eid = row.get("event_id")
        if eid in seen_events:
            continue
        start_str = row.get("event_start_time")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if not (start_dt >= now and start_dt.date() == now.date()):
            continue
        seen_events.add(eid)
        away, home = row.get("away_team"), row.get("home_team")
        sharp_today[eid] = {
            "away_team": away, "home_team": home,
            "away_normalized": worker.normalize_team_name(away),
            "home_normalized": worker.normalize_team_name(home),
        }

    return {
        "league": league,
        "sharpapi_today_games": sharp_today,
        "kalshi_open_events": kalshi_side,
    }


@app.route("/debug_find_team")
def debug_find_team_route():
    """
    Searches ALL fetched SharpAPI rows (not just today's) for a team name
    substring, case-insensitive -- to answer "does SharpAPI even carry
    this game" separately from "did our matching logic fail on it".
    ?league=mlb&team=dodgers
    """
    import worker
    league = request.args.get("league", "mlb")
    team = request.args.get("team", "").lower()
    rows = worker.fetch_sharpapi_odds(league)
    seen = {}
    for row in rows:
        away, home = row.get("away_team") or "", row.get("home_team") or ""
        if team in away.lower() or team in home.lower():
            eid = row.get("event_id")
            seen[eid] = {
                "away_team": away, "home_team": home,
                "event_start_time": row.get("event_start_time"),
                "sportsbook": row.get("sportsbook"), "market_type": row.get("market_type"),
                "is_main_line": row.get("is_main_line"),
            }
    return {"league": league, "team": team, "total_rows_scanned": len(rows), "matches": seen}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
