import os
import json
from flask import Flask, render_template_string, request, redirect
from pykalshi import KalshiClient

import ledger

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")

app = Flask(__name__)

PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AR894 Dashboard</title>
<style>
  body { background:#0d1117; color:#e6edf3; font-family:-apple-system,sans-serif; padding:16px; margin:0; }
  h1 { font-size:1.3em; margin-bottom:4px; }
  h2 { font-size:1.05em; margin-top:28px; margin-bottom:8px; color:#8b949e; border-bottom:1px solid #21262d; padding-bottom:6px; }
  .balance { font-size:2em; font-weight:700; margin:8px 0; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:10px; padding:12px; margin-bottom:10px; }
  .row { display:flex; justify-content:space-between; align-items:center; }
  .green { color:#3fb950; }
  .red { color:#f85149; }
  .muted { color:#8b949e; font-size:0.85em; }
  .badge { font-size:0.75em; padding:2px 8px; border-radius:12px; background:#21262d; }
  table { width:100%; border-collapse:collapse; font-size:0.85em; }
  td { padding:6px 4px; border-bottom:1px solid #21262d; }
  .alert { background:#3b2205; border:1px solid #9e6a03; border-radius:10px; padding:14px; margin-bottom:16px; }
  input[type=number] { width:100%; padding:10px; border-radius:8px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; font-size:1em; margin:8px 0; box-sizing:border-box; }
  button { width:100%; padding:12px; border-radius:8px; border:none; background:#238636; color:white; font-size:1em; font-weight:600; }
</style>
</head>
<body>
  <h1>AR894 Dashboard</h1>
  <div class="muted">Updated on every page load</div>

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

  <div class="balance">${{ "%.2f"|format(balance) }}</div>
  <div class="muted">Kalshi account balance</div>

  <h2>Budget</h2>
  <div class="card">
    <div class="row"><span>Total allocated (ever)</span><span>${{ "%.2f"|format(total_allocated) }}</span></div>
    <div class="row"><span>Currently committed</span><span>${{ "%.2f"|format(committed) }}</span></div>
    <div class="row"><span><strong>Available to trade</strong></span><span><strong>${{ "%.2f"|format(available_budget) }}</strong></span></div>
    <div class="row"><span>Bot's realized profit (not spendable)</span>
      <span class="{{ 'green' if realized_profit >= 0 else 'red' }}">${{ "%.2f"|format(realized_profit) }}</span>
    </div>
  </div>

  <h2>Open Positions ({{ positions|length }})</h2>
  {% if positions %}
    {% for p in positions %}
    <div class="card">
      <div class="row"><strong>{{ p.ticker }}</strong><span class="badge">{{ p.position_fp }} contracts</span></div>
      <div class="muted">Exposure: ${{ p.market_exposure_dollars }}</div>
    </div>
    {% endfor %}
  {% else %}
    <div class="muted">No open positions right now.</div>
  {% endif %}

  <h2>Recent Real Trades</h2>
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
    <div class="muted">No real trades logged yet.</div>
  {% endif %}

  <h2>Paper Trading: Moneyline</h2>
  <div class="card">
    <div class="row"><span>Total picks</span><span>{{ ml_summary.total_picks }}</span></div>
    <div class="row"><span>Resolved</span><span>{{ ml_summary.resolved }}</span></div>
    {% if ml_summary.resolved > 0 %}
    <div class="row"><span>Win rate</span><span>{{ "%.1f"|format(ml_summary.win_rate) }}%</span></div>
    <div class="row"><span>Hypothetical P&L</span>
      <span class="{{ 'green' if (ml_summary.total_hypothetical_pnl or 0) >= 0 else 'red' }}">
        ${{ "%.2f"|format(ml_summary.total_hypothetical_pnl or 0) }}
      </span>
    </div>
    {% endif %}
  </div>

  <h2>Paper Trading: BTC 15min</h2>
  <div class="card">
    <div class="row"><span>Total picks</span><span>{{ btc_summary.total_picks }}</span></div>
    <div class="row"><span>Resolved</span><span>{{ btc_summary.resolved }}</span></div>
    {% if btc_summary.resolved > 0 %}
    <div class="row"><span>Win rate</span><span>{{ "%.1f"|format(btc_summary.win_rate) }}%</span></div>
    <div class="row"><span>Hypothetical P&L</span>
      <span class="{{ 'green' if (btc_summary.total_hypothetical_pnl or 0) >= 0 else 'red' }}">
        ${{ "%.2f"|format(btc_summary.total_hypothetical_pnl or 0) }}
      </span>
    </div>
    {% endif %}
  </div>

  <div class="muted" style="margin-top:24px; text-align:center;">AR894 Engine</div>
</body>
</html>
"""


def load_json(filename, default):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def get_client():
    os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
    os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])
    return KalshiClient()


@app.route("/")
def dashboard():
    import paper_trading as pt

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

    return render_template_string(
        PAGE_TEMPLATE,
        balance=balance,
        positions=positions,
        trade_log=trade_log,
        ml_summary=summary["moneyline"],
        btc_summary=summary["btc"],
        total_allocated=led["total_allocated"],
        committed=committed,
        available_budget=available_budget,
        realized_profit=realized_profit,
        pending_deposit=led.get("pending_deposit_amount"),
    )


@app.route("/allocate", methods=["POST"])
def allocate():
    amount = float(request.form.get("amount", 0))
    ledger.approve_allocation(amount)
    return redirect("/")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
