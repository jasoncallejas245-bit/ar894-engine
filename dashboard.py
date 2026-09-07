import os
import json
from flask import Flask, render_template_string
from pykalshi import KalshiClient

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
</style>
</head>
<body>
  <h1>AR894 Dashboard</h1>
  <div class="muted">Updated on every page load</div>

  <div class="balance">${{ "%.2f"|format(balance) }}</div>
  <div class="muted">Kalshi account balance</div>

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
      <tr><td><b>League</b></td><td><b>Matchup</b></td><td><b>Edge</b></td><td><b>Stake</b></td></tr>
      {% for t in trade_log[-10:]|reverse %}
      <tr>
        <td>{{ t.league }}</td>
        <td>{{ t.matchup }}</td>
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


@app.route("/")
def dashboard():
    import paper_trading as pt

    os.environ.setdefault("KALSHI_API_KEY_ID", os.environ["KALSHI_KEY_ID"])
    os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", os.environ["KALSHI_PRIVATE_KEY_PATH"])
    client = KalshiClient()

    try:
        balance = client.portfolio.get_balance().balance / 100.0
    except Exception:
        balance = 0.0

    try:
        raw_positions = client.portfolio.get_positions()
        positions = [p for p in raw_positions if float(getattr(p, "position_fp", 0) or 0) != 0]
    except Exception:
        positions = []

    trade_log = load_json("trade_audit_log.json", [])
    summary = pt.get_paper_trade_summary()

    return render_template_string(
        PAGE_TEMPLATE,
        balance=balance,
        positions=positions,
        trade_log=trade_log,
        ml_summary=summary["moneyline"],
        btc_summary=summary["btc"],
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
