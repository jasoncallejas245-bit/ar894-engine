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
    {% if real_trading_on %}LIVE: real money on {{ real_trading_summary }}{% else %}PAUSED — 100% paper trading, no real money at risk{% endif %}
  </div>

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
      <h3>{{ c.label }} · ${{ "%.0f"|format(paper_stake) }}/pick</h3>
      <div class="big {{ 'green' if not c.bankroll_down else 'red' }}">
        ${{ "%.2f"|format(c.bankroll_balance) }}
      </div>
      <div class="sub">{% if c.bankroll_down %}down ${{ "%.2f"|format(c.bankroll_down_by) }} from ${{ "%.0f"|format(paper_starting_bankroll) }} start{% else %}up from ${{ "%.0f"|format(paper_starting_bankroll) }} start{% endif %}</div>

      <div class="row" style="margin-top:12px;">
        <span class="label">Win rate</span>
        <span>{% if c.summary.resolved > 0 %}{{ "%.0f"|format(c.summary.win_rate) }}% ({{ c.summary.resolved }} resolved){% else %}no resolved picks yet{% endif %}</span>
      </div>
      <div class="row">
        <span class="label">{{ c.setting_label }}</span>
        <span>{{ c.setting_value }}</span>
      </div>

      <div class="row" style="margin-top:10px;">
        <span class="label">Learning progress</span>
        <span class="muted">{{ c.sample }}/{{ min_sample }}</span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:{{ (100 * c.sample / min_sample)|round(0, 'floor')|int if c.sample < min_sample else 100 }}%;"></div></div>
    </div>
    {% endfor %}
  </div>

  <div class="card">
    <h3>Too Close To Call — Context</h3>
    {% if too_close_picks %}
      {% for p in too_close_picks[:6] %}
      <div class="row" style="align-items:flex-start; margin-bottom:10px; border-bottom:1px solid #21262d; padding-bottom:10px;">
        <div>
          <div><strong>{{ p.picked_team }}</strong> <span class="badge">{{ p.league }}</span></div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · fair {{ "%.0f"|format(p.market_probability*100) }}% · {{ p.status }}</div>
          {% if p.context_note %}
            <div class="sub" style="white-space:pre-line; margin-top:4px;">{{ p.context_note }}</div>
          {% else %}
            <div class="sub" style="margin-top:4px;">No injury/weather flags found.</div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">No borderline picks yet — these are picks that clear the favorite bar but only just.</div>
    {% endif %}
  </div>

  {% if adaptive.last_adjusted %}
  <div class="muted" style="text-align:center; margin-bottom:8px;">Learning system last adjusted something: {{ adaptive.last_adjusted }}</div>
  {% endif %}

  <details>
    <summary>Real-money account &amp; trade history</summary>

    <div class="card">
      <div class="row"><span class="label">Kalshi balance</span><span>${{ "%.2f"|format(balance) }}</span></div>
      <div class="row"><span class="label">Available to trade</span><span>${{ "%.2f"|format(available_budget) }}</span></div>
      <div class="row"><span class="label">Currently committed</span><span>${{ "%.2f"|format(committed) }}</span></div>
      <div class="row"><span class="label">Bot's realized profit</span>
        <span class="{{ 'green' if realized_profit >= 0 else 'red' }}">${{ "%.2f"|format(realized_profit) }}</span>
      </div>
    </div>

    <div class="card">
      <h3>Open Positions ({{ positions|length }})</h3>
      {% if positions %}
        {% for p in positions %}
        <div class="row"><span>{{ p.ticker }}</span><span class="badge">{{ p.position_fp }} contracts</span></div>
        {% endfor %}
      {% else %}
        <div class="muted">None right now.</div>
      {% endif %}
    </div>

    <div class="card">
      <h3>Recent Real Trades</h3>
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

    categories = [
        {
            "key": "moneyline", "label": "Moneyline",
            "summary": summary["moneyline"],
            "bankroll_balance": ml_bank["balance"],
            "bankroll_down": ml_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - ml_bank["balance"]),
            "setting_label": "Favorite bar",
            "setting_value": favorite_note,
            "sample": min(adaptive.get("moneyline_sample_size", 0), min_sample),
        },
        {
            "key": "btc", "label": "BTC 15min",
            "summary": summary["btc"],
            "bankroll_balance": btc_bank["balance"],
            "bankroll_down": btc_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - btc_bank["balance"]),
            "setting_label": "Momentum window",
            "setting_value": f"{btc_window} readings",
            "sample": min(adaptive.get("btc_sample_size", 0), min_sample),
        },
    ]

    all_moneyline_picks = pt.load_paper_trades().get("moneyline", [])
    too_close_picks = [p for p in all_moneyline_picks if p.get("is_too_close")][-15:][::-1]

    real_trading_on = bool(worker.REAL_TRADING_LEAGUES) or worker.BTC_REAL_TRADING_ENABLED
    real_trading_summary = ", ".join(sorted(worker.REAL_TRADING_LEAGUES)) if worker.REAL_TRADING_LEAGUES else ""
    if worker.BTC_REAL_TRADING_ENABLED:
        real_trading_summary = (real_trading_summary + " + BTC").strip(" +")

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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
