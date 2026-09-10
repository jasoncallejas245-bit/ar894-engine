import os
from datetime import datetime, timezone
from flask import Flask, render_template_string, request, redirect
from pykalshi import KalshiClient

import ledger
from state_io import safe_read_json

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")

app = Flask(__name__)


def _build_pnl_chart_svg(points, width=560, height=140):
    """
    Small dependency-free SVG line chart of cumulative hypothetical P&L
    across every resolved paper pick, oldest to newest -- a PrizePicks-
    style "how am I doing" trend line, rendered server-side so the
    dashboard doesn't need a JS charting library. `points` is a list of
    running totals, starting at 0. Never raises -- returns a placeholder
    SVG on bad input rather than crashing the whole page.
    """
    try:
        if not points or len(points) < 2:
            return (
                '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" style="display:block;">'
                '<text x="{cx}" y="{cy}" text-anchor="middle" fill="#8b949e" font-size="13">'
                'Not enough resolved picks yet for a trend line.</text></svg>'
            ).format(w=width, h=height, cx=width / 2, cy=height / 2)

        pad = 10
        lo, hi = min(points), max(points)
        span = (hi - lo) or 1.0
        n = len(points)

        def x_of(i):
            return pad + (i / (n - 1)) * (width - 2 * pad)

        def y_of(v):
            return pad + (1 - (v - lo) / span) * (height - 2 * pad)  # inverted -- SVG y grows downward

        coords = [(x_of(i), y_of(v)) for i, v in enumerate(points)]
        path_d = "M " + " L ".join("{:.1f},{:.1f}".format(x, y) for x, y in coords)

        zero_y = y_of(0.0) if lo <= 0.0 <= hi else None
        baseline_y = zero_y if zero_y is not None else (height - pad)
        zero_line = ""
        if zero_y is not None:
            zero_line = (
                '<line x1="{p}" y1="{y:.1f}" x2="{w}" y2="{y:.1f}" '
                'stroke="#30363d" stroke-width="1" stroke-dasharray="4,4" />'
            ).format(p=pad, w=width - pad, y=zero_y)

        final_v = points[-1]
        line_color = "#3fb950" if final_v >= 0 else "#f85149"
        fill_id = "pnlFillPos" if final_v >= 0 else "pnlFillNeg"
        last_x, last_y = coords[-1]
        first_x = coords[0][0]

        area_d = "{path} L {lx:.1f},{by:.1f} L {fx:.1f},{by:.1f} Z".format(
            path=path_d, lx=last_x, by=baseline_y, fx=first_x
        )

        svg_parts = []
        svg_parts.append('<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" style="display:block; overflow:visible;">'.format(w=width, h=height))
        svg_parts.append('<defs><linearGradient id="{fid}" x1="0" y1="0" x2="0" y2="1">'.format(fid=fill_id))
        svg_parts.append('<stop offset="0%" stop-color="{c}" stop-opacity="0.28" />'.format(c=line_color))
        svg_parts.append('<stop offset="100%" stop-color="{c}" stop-opacity="0" />'.format(c=line_color))
        svg_parts.append('</linearGradient></defs>')
        svg_parts.append(zero_line)
        svg_parts.append('<path d="{d}" fill="url(#{fid})" stroke="none" />'.format(d=area_d, fid=fill_id))
        svg_parts.append('<path d="{d}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'.format(d=path_d, c=line_color))
        svg_parts.append('<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{c}" />'.format(x=last_x, y=last_y, c=line_color))
        svg_parts.append('</svg>')
        return "".join(svg_parts)
    except Exception as e:
        print(f"[dashboard] chart render failed: {e}")
        return '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}"></svg>'.format(w=width, h=height)

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
<title>Picks Dashboard</title>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    background:#0a0e14; background-image:radial-gradient(circle at 15% 0%, rgba(88,101,242,0.10), transparent 40%), radial-gradient(circle at 85% 8%, rgba(63,185,80,0.07), transparent 35%);
    color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    padding:16px 16px 40px; margin:0; max-width:640px; margin-left:auto; margin-right:auto;
    -webkit-font-smoothing:antialiased;
  }
  .header { display:flex; align-items:center; gap:12px; margin:4px 0 18px 0; }
  .logo { width:38px; height:38px; border-radius:11px; flex-shrink:0; background:linear-gradient(135deg,#6e7bff,#3fb950); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:1.05em; color:#0a0e14; box-shadow:0 4px 14px rgba(88,101,242,0.35); }
  h1 { font-size:1.25em; margin:0; letter-spacing:-0.01em; font-weight:700; }
  .tagline { font-size:0.78em; color:#8b949e; margin-top:1px; }
  h3 { font-size:0.74em; text-transform:uppercase; letter-spacing:0.06em; color:#8b949e; margin:0 0 10px 0; font-weight:700; }
  .status { display:flex; align-items:center; gap:9px; padding:11px 15px; border-radius:12px; font-weight:600; font-size:0.9em; margin-bottom:16px; box-shadow:0 2px 10px rgba(0,0,0,0.18); }
  .status.paused { background:linear-gradient(135deg,#3b2a05,#2a1e07); color:#e3b341; border:1px solid #9e6a03; }
  .status.live { background:linear-gradient(135deg,#0d2818,#0e1f16); color:#3fb950; border:1px solid #238636; }
  .dot { width:8px; height:8px; border-radius:50%; background:currentColor; flex-shrink:0; box-shadow:0 0 8px currentColor; }
  .card {
    background:#12161f; border:1px solid #232a36; border-radius:16px; padding:17px;
    margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,0.3); transition:border-color 0.15s ease;
  }
  .row { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; font-size:0.9em; gap:10px; }
  .row:last-child { margin-bottom:0; }
  .row .label { color:#8b949e; }
  .big { font-size:1.95em; font-weight:800; line-height:1.1; font-variant-numeric:tabular-nums; letter-spacing:-0.01em; }
  .green { color:#3fb950; }
  .red { color:#f85149; }
  .muted { color:#8b949e; font-size:0.82em; }
  .sub { color:#8b949e; font-size:0.8em; margin-top:2px; line-height:1.4; }
  .badge { font-size:0.71em; padding:3px 9px; border-radius:20px; background:#1c2333; color:#9aa6c7; border:1px solid #2a3348; font-weight:600; }
  .badge-bet { background:#0d2818; color:#3fb950; border:1px solid #238636; }
  .badge-thin { background:#2a2410; color:#d4a72c; border:1px solid #5a4a15; }
  .badge-data { background:#1c2230; color:#6e7a94; border:1px solid #2a3348; }
  .score-bar { background:#1c2230; border-radius:8px; height:5px; overflow:hidden; margin-top:6px; }
  .score-bar-fill { height:100%; border-radius:8px; transition:width 0.3s ease; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width:480px) { .grid2 { grid-template-columns:1fr; } }
  .bar { background:#1c2230; border-radius:8px; height:7px; overflow:hidden; margin-top:9px; }
  .bar-fill { height:100%; border-radius:8px; background:linear-gradient(90deg,#6e7bff,#58a6ff); transition:width 0.3s ease; }
  .alert { background:linear-gradient(135deg,#3b2a05,#2a1e07); border:1px solid #9e6a03; border-radius:14px; padding:15px; margin-bottom:16px; box-shadow:0 2px 10px rgba(0,0,0,0.18); }
  table { width:100%; border-collapse:collapse; font-size:0.82em; }
  td { padding:6px 4px; border-bottom:1px solid #1f2530; }
  input[type=number] { width:100%; padding:11px; border-radius:9px; border:1px solid #2a3348; background:#0a0e14; color:#e6edf3; font-size:1em; margin:8px 0; box-sizing:border-box; }
  input[type=number]:focus { outline:none; border-color:#6e7bff; }
  button { width:100%; padding:13px; border-radius:9px; border:none; background:linear-gradient(135deg,#2ea043,#238636); color:white; font-size:1em; font-weight:700; letter-spacing:0.01em; box-shadow:0 2px 10px rgba(35,134,54,0.3); }
  button:active { transform:scale(0.99); }
  details { margin-top:16px; }
  summary { cursor:pointer; color:#8b949e; font-size:0.82em; padding:8px 0; font-weight:600; }
  summary:hover { color:#e6edf3; }
</style>
</head>
<body>
  <div class="header">
    <div class="logo">P</div>
    <div>
      <h1>Picks</h1>
      <div class="tagline">Automated moneyline &amp; prop picks, live P&amp;L tracker</div>
    </div>
  </div>

  <div class="status {{ 'live' if real_trading_on else 'paused' }}">
    <span class="dot"></span>
    {% if real_trading_on %}Using real money on: {{ real_trading_summary }}{% else %}Practice mode — no real money is being risked right now{% endif %}
  </div>
  <div class="muted" style="margin:-10px 0 16px 0;">{% if real_trading_on %}The bot is placing real bets with real dollars on the leagues listed above.{% else %}Everything below is a simulation — it tracks what WOULD happen so the strategy can be tested safely before any real money is used.{% endif %}</div>

  <div class="card" style="padding:12px 16px;">
    <div class="row">
      <span class="label">Right now</span>
      <span>{{ activity.status_text }}</span>
    </div>
    <div class="row">
      <span class="label">Next scan</span>
      <span>{{ activity.next_scan_text }}</span>
    </div>
    {% if activity.recent_errors %}
    <details style="margin-top:8px;">
      <summary>{{ activity.recent_errors|length }} recent hiccup(s) — tap to see</summary>
      {% for err in activity.recent_errors %}
      <div class="sub" style="margin-top:6px; padding-top:6px; border-top:1px solid #21262d;">{{ err.at }} — {{ err.message }}</div>
      {% endfor %}
    </details>
    {% else %}
    <div class="sub" style="margin-top:4px;">No errors in the last {{ activity.error_window_label }}.</div>
    {% endif %}
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

  <div class="card">
    <h3>Profitability Status -- All Strategies</h3>
    <div class="sub" style="margin-bottom:10px;">Crosses BOTH a real sample size ({{ min_sample }}+ resolved picks) and genuine profit before counting as "profitable" here -- same bar the Discord alerts use.</div>
    {% for p in profitability_status %}
    <div class="row" style="align-items:flex-start; margin-bottom:10px; border-bottom:1px solid #21262d; padding-bottom:10px;">
      <div>
        <div><strong>{{ p.label }}</strong></div>
        <div class="sub">{{ p.resolved }} of {{ p.min_sample }} resolved{% if p.win_rate is not none %} · {{ "%.0f"|format(p.win_rate) }}% correct{% endif %}{% if p.pnl is not none %} · ${{ "%.2f"|format(p.pnl) }} hypothetical{% endif %}</div>
      </div>
      <div style="text-align:right; white-space:nowrap;">
        {% if p.is_profitable and p.can_go_real and p.real_on %}
          <span class="green">✓ Profitable — real trading ON</span>
        {% elif p.is_profitable and p.can_go_real %}
          <span class="green">✓ PROFITABLE — fund &amp; turn on real trading</span>
        {% elif p.is_profitable %}
          <span class="green">✓ Profitable (paper-only forever)</span>
        {% elif p.resolved < p.min_sample %}
          <span class="muted">Gathering data</span>
        {% else %}
          <span class="red">Not profitable yet</span>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="card">
    <h3>📈 Performance -- Cumulative Hypothetical P&amp;L</h3>
    <div class="sub" style="margin-bottom:10px;">Every resolved paper pick across all strategies, oldest to newest -- same idea as a PrizePicks results chart, just built from our own paper-trade ledger.</div>
    <div class="row" style="margin-bottom:10px;">
      <div>
        <span class="label">Net P&amp;L</span>
        <span class="big {{ 'green' if net_pnl >= 0 else 'red' }}" style="font-size:1.3em; margin-left:6px;">${{ "%.2f"|format(net_pnl) }}</span>
      </div>
      <div class="sub">{{ total_resolved_count }} resolved pick{{ '' if total_resolved_count == 1 else 's' }}</div>
    </div>
    {{ chart_svg|safe }}

    {% if parlay_leg_breakdown %}
    <div style="margin-top:16px;">
      <div class="sub" style="margin-bottom:6px;">Parlay leg-count breakdown -- which combo size is actually working:</div>
      {% for b in parlay_leg_breakdown %}
      <div class="row" style="border-bottom:1px solid #21262d; padding-bottom:6px; margin-bottom:6px;">
        <span>{{ b.leg_count }}-leg</span>
        <span class="sub">{{ b.resolved }} resolved{% if b.win_rate is not none %} · {{ "%.0f"|format(b.win_rate) }}% won{% endif %}</span>
        <span class="{{ 'green' if b.total_pnl >= 0 else 'red' }}">${{ "%.2f"|format(b.total_pnl) }}</span>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>

  <div class="card">
    <h3>Your Manual Bets</h3>
    <div class="sub" style="margin-bottom:10px;">Real bets you placed yourself with your own money on Kalshi. Log one against a pick below and this tracks how your own betting actually does -- including whether following a \U0001F525 strong pick beats picking your own from the \U0001F44D thin-edge pool.</div>

    <form method="POST" action="/log_manual_bet" style="display:flex; flex-direction:column; gap:8px; margin-bottom:16px; padding:12px; background:#161b26; border-radius:8px; border:1px solid #2a3348;">
      <select name="pick_id" required style="background:#0d1117; color:#e6edf3; border:1px solid #2a3348; border-radius:6px; padding:8px;">
        <option value="" disabled selected>Which pick did you bet?</option>
        {% for lp in loggable_picks %}
        <option value="{{ lp.pick_id }}">{{ '\U0001F525' if lp.bet_tier == 'strong' else ('\U0001F44D' if lp.bet_tier == 'thin' else '⚠') }} {{ lp.label }}</option>
        {% endfor %}
      </select>
      <div style="display:flex; gap:8px;">
        <input type="number" name="stake_dollars" placeholder="Your $ stake" min="1" step="0.01" required style="flex:1; background:#0d1117; color:#e6edf3; border:1px solid #2a3348; border-radius:6px; padding:8px;">
        <button type="submit" style="background:#238636; color:#fff; border:none; border-radius:6px; padding:8px 16px; font-weight:600; cursor:pointer;">Log bet</button>
      </div>
      <input type="text" name="note" placeholder="Optional note" style="background:#0d1117; color:#e6edf3; border:1px solid #2a3348; border-radius:6px; padding:8px;">
    </form>

    <div class="row" style="margin-bottom:6px;">
      <span class="label">Overall</span>
      <span>{{ manual_bet_summary.overall.resolved }}/{{ manual_bet_summary.overall.logged }} resolved{% if manual_bet_summary.overall.win_rate is not none %} · {{ "%.0f"|format(manual_bet_summary.overall.win_rate) }}% won{% endif %} · <span class="{{ 'green' if manual_bet_summary.overall.pnl >= 0 else 'red' }}">${{ "%.2f"|format(manual_bet_summary.overall.pnl) }}</span></span>
    </div>
    <div class="row" style="margin-bottom:6px;">
      <span class="label">\U0001F525 Followed a strong pick</span>
      <span>{{ manual_bet_summary.followed_recommendation.resolved }}/{{ manual_bet_summary.followed_recommendation.logged }} resolved{% if manual_bet_summary.followed_recommendation.win_rate is not none %} · {{ "%.0f"|format(manual_bet_summary.followed_recommendation.win_rate) }}% won{% endif %} · <span class="{{ 'green' if manual_bet_summary.followed_recommendation.pnl >= 0 else 'red' }}">${{ "%.2f"|format(manual_bet_summary.followed_recommendation.pnl) }}</span></span>
    </div>
    <div class="row" style="margin-bottom:10px;">
      <span class="label">Picked on your own</span>
      <span>{{ manual_bet_summary.own_choice.resolved }}/{{ manual_bet_summary.own_choice.logged }} resolved{% if manual_bet_summary.own_choice.win_rate is not none %} · {{ "%.0f"|format(manual_bet_summary.own_choice.win_rate) }}% won{% endif %} · <span class="{{ 'green' if manual_bet_summary.own_choice.pnl >= 0 else 'red' }}">${{ "%.2f"|format(manual_bet_summary.own_choice.pnl) }}</span></span>
    </div>

    {% if manual_bets_list %}
    {% for m in manual_bets_list %}
    <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
      <div>
        <div><strong>{{ m.label }}</strong> <span class="badge">${{ "%.2f"|format(m.stake_dollars) }}</span></div>
        <div class="sub">{{ m.logged_at }}{% if m.note %} · {{ m.note }}{% endif %}</div>
      </div>
      <div class="{{ 'green' if m.status == 'won' else ('red' if m.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
        {% if m.result_pnl is not none %}${{ "%.2f"|format(m.result_pnl) }}{% else %}{{ m.status }}{% endif %}
      </div>
    </div>
    {% endfor %}
    {% else %}
    <div class="muted">No manual bets logged yet -- use the form above after you place one.</div>
    {% endif %}
  </div>

  <div class="card" style="border:1px solid #3a4a6b; background:linear-gradient(160deg,#141a2c,#12161f);">
    <h3 style="color:#8fa8ff;">🏈 Passing Yards -- Main Focus</h3>
    <div class="sub" style="margin-bottom:10px;">NFL/NCAAF quarterback passing-yards picks -- PRACTICE BETS OF $15 EACH -- each graded independently (not bundled into an all-or-nothing ticket) -- built for volume so a real track record shows up fast.</div>
    <div class="row">
      <span class="label">Picks made</span>
      <span class="big" style="font-size:1.3em;">{{ passing_yards_summary.total_picks or 0 }}</span>
    </div>
    <div class="row">
      <span class="label">Resolved / correct</span>
      <span>{{ passing_yards_summary.resolved or 0 }} resolved{% if passing_yards_summary.win_rate is not none %} · {{ "%.0f"|format(passing_yards_summary.win_rate) }}% correct{% endif %}</span>
    </div>
    <div class="row">
      <span class="label">Paper bankroll</span>
      <span class="{{ 'red' if passing_yards_bank.balance < 100 else 'green' }}">${{ "%.2f"|format(passing_yards_bank.balance) }}</span>
    </div>
    <div class="bar"><div class="bar-fill" style="width:{{ (100 * (passing_yards_summary.resolved or 0) / min_sample)|round(0, 'floor')|int if (passing_yards_summary.resolved or 0) < min_sample else 100 }}%;"></div></div>
    <div class="sub" style="margin-top:8px;">{{ passing_yards_summary.resolved or 0 }} of {{ min_sample }} needed before this counts toward the profitability check above.</div>

    {% if all_passing_yards_picks %}
    <div style="margin-top:14px;">
      {% for p in all_passing_yards_picks[:8] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ p.player }}</strong> <span class="badge">{{ p.league }}</span> {{ p.side|upper }} {{ p.line }} yds{% if p.get('bet_tier') == 'strong' %} <span class="badge badge-bet">🔥 strong</span>{% elif p.get('bet_tier') == 'thin' %} <span class="badge badge-thin">👍 thin edge</span>{% endif %}</div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · {{ "%.0f"|format((p.consensus_prob or 0)*100) }}% probability to hit · <strong>$15 bet</strong>{% if p.get('potential_payout') is not none %} · pays <strong class="green">+${{ "%.2f"|format(p.potential_payout) }}</strong> if it hits{% endif %}{% if p.get('final_value') is not none %} · actual {{ "%.0f"|format(p.final_value) }} yds{% endif %}</div>
          <div class="score-bar"><div class="score-bar-fill" style="width:{{ p.get('pick_score', 0) }}%; background:hsl({{ (p.get('pick_score', 0) * 1.2)|round(0, 'floor')|int }}, 70%, 45%);"></div></div>
        </div>
        <div class="{{ 'green' if p.status == 'won' else ('red' if p.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
          {% if p.get('hypothetical_pnl') is not none %}${{ "%.2f"|format(p.get('hypothetical_pnl')) }}{% else %}{{ p.status }}{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
      <div class="muted" style="margin-top:10px;">No passing-yards picks yet -- shows up here as soon as NFL/NCAAF games have prop lines listed.</div>
    {% endif %}
  </div>

  <div class="card" style="border:1px solid #3a4a6b; background:linear-gradient(160deg,#141a2c,#12161f);">
    <h3 style="color:#8fa8ff;">🏀 WNBA Combined Stats -- Main Focus</h3>
    <div class="sub" style="margin-bottom:10px;">WNBA combo props (points+rebounds+assists-style multi-stat lines) -- PRACTICE BETS OF $15 EACH -- each graded independently, same high-volume approach as Passing Yards. <strong>WNBA games run more volatile than the other leagues here</strong> -- fewer possessions and bigger swings per play than NBA/NFL, worth weighing that in before betting one yourself even on a 🔥 strong pick.</div>
    <div class="row">
      <span class="label">Picks made</span>
      <span class="big" style="font-size:1.3em;">{{ wnba_combined_summary.total_picks or 0 }}</span>
    </div>
    <div class="row">
      <span class="label">Resolved / correct</span>
      <span>{{ wnba_combined_summary.resolved or 0 }} resolved{% if wnba_combined_summary.win_rate is not none %} · {{ "%.0f"|format(wnba_combined_summary.win_rate) }}% correct{% endif %}</span>
    </div>
    <div class="row">
      <span class="label">Paper bankroll</span>
      <span class="{{ 'red' if wnba_combined_bank.balance < 100 else 'green' }}">${{ "%.2f"|format(wnba_combined_bank.balance) }}</span>
    </div>
    <div class="bar"><div class="bar-fill" style="width:{{ (100 * (wnba_combined_summary.resolved or 0) / min_sample)|round(0, 'floor')|int if (wnba_combined_summary.resolved or 0) < min_sample else 100 }}%;"></div></div>
    <div class="sub" style="margin-top:8px;">{{ wnba_combined_summary.resolved or 0 }} of {{ min_sample }} needed before this counts toward the profitability check above.</div>

    {% if all_wnba_combined_picks %}
    <div style="margin-top:14px;">
      {% for p in all_wnba_combined_picks[:8] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ p.player }}</strong> <span class="badge">{{ p.league }}</span> {{ p.side|upper }} {{ p.line }} {{ p.stat_type }}{% if p.get('bet_tier') == 'strong' %} <span class="badge badge-bet">🔥 strong</span>{% elif p.get('bet_tier') == 'thin' %} <span class="badge badge-thin">👍 thin edge</span>{% endif %}</div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · {{ "%.0f"|format((p.consensus_prob or 0)*100) }}% probability to hit · <strong>$15 bet</strong>{% if p.get('potential_payout') is not none %} · pays <strong class="green">+${{ "%.2f"|format(p.potential_payout) }}</strong> if it hits{% endif %}{% if p.get('final_value') is not none %} · actual {{ "%.0f"|format(p.final_value) }}{% endif %}</div>
          <div class="score-bar"><div class="score-bar-fill" style="width:{{ p.get('pick_score', 0) }}%; background:hsl({{ (p.get('pick_score', 0) * 1.2)|round(0, 'floor')|int }}, 70%, 45%);"></div></div>
        </div>
        <div class="{{ 'green' if p.status == 'won' else ('red' if p.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
          {% if p.get('hypothetical_pnl') is not none %}${{ "%.2f"|format(p.get('hypothetical_pnl')) }}{% else %}{{ p.status }}{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
      <div class="muted" style="margin-top:10px;">No WNBA combined-stat picks yet -- shows up here as soon as WNBA games have combo prop lines listed.</div>
    {% endif %}
  </div>

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
      {% if c.note %}
      <div class="sub" style="margin-top:8px; border-top:1px solid #21262d; padding-top:8px;">⚠ {{ c.note }}</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  <div class="card">
    <h3>Close Calls — Extra Info</h3>
    <div class="sub" style="margin-bottom:10px;">These ARE real picks the bot made (not a separate or pending category) — just the subset that only barely cleared the "safe enough to bet" bar, basically a coin flip with a slight edge. Every pick (including the clear favorites, shown below in "All Recent Picks") now gets the same injury/matchup/pitcher context — this card just highlights the ones where that context mattered most.</div>
    {% if too_close_picks %}
      {% for p in too_close_picks[:6] %}
      <div class="row" style="align-items:flex-start; margin-bottom:10px; border-bottom:1px solid #21262d; padding-bottom:10px;">
        <div>
          <div><strong>{{ p.picked_team }}</strong> <span class="badge">{{ p.league }}</span></div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · {{ "%.0f"|format(p.market_probability*100) }}% likely to win · {{ p.status }}{% if p.starts_in %} · game {{ p.starts_in }}{% endif %}</div>
          {% if p.context_note %}
            <div class="sub" style="white-space:pre-line; margin-top:4px;">{{ p.context_note }}</div>
          {% else %}
            <div class="sub" style="margin-top:4px;">Nothing notable found.</div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">None right now — no picks have been this close to a coin flip yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>All Recent Picks</h3>
    <div class="sub" style="margin-bottom:10px;">Every moneyline pick the bot has made recently, clear favorites and close calls alike, whatever its result. This is the full picture -- if it's not here, it isn't a pick the bot made. <span class="green">🔥 Strong</span> clears the tighter bar real trading uses. <span style="color:#d4a72c;">👍 Thin edge</span> has real detected edge, just under that bar -- still worth a look, smaller size if you take it. <span class="muted">⚠ Skip</span> is a near-zero-edge pick kept only to build up data volume -- not worth your own money. The bar under each pick is the same signal as a % gauge, red to green.</div>
    {% if all_recent_picks %}
      {% for p in all_recent_picks[:15] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ p.picked_team }}</strong> <span class="badge">{{ p.league }}</span>{% if p.is_too_close %} <span class="badge">close call</span>{% endif %}{% set _tier = p.get('bet_tier') or ('strong' if p.get('manual_bet_candidate') else 'skip') %}{% if _tier == 'strong' %} <span class="badge badge-bet">🔥 strong -- bet this</span>{% elif _tier == 'thin' %} <span class="badge badge-thin">👍 thin edge -- your call</span>{% else %} <span class="badge badge-data">⚠ skip -- no edge</span>{% endif %}</div>
          <div class="sub">{{ p.away_team }} @ {{ p.home_team }} · contract price ${{ "%.2f"|format(p.entry_price or 0) }} ({{ "%.0f"|format((p.entry_price or 0)*100) }}% implied) · <strong>$15 bet</strong>{% if p.get('potential_payout') is not none %} · pays <strong class="green">+${{ "%.2f"|format(p.potential_payout) }}</strong> if it hits{% endif %} · {{ p.status }}</div>
          <div class="score-bar"><div class="score-bar-fill" style="width:{{ p.get('pick_score', 0) }}%; background:hsl({{ (p.get('pick_score', 0) * 1.2)|round(0, 'floor')|int }}, 70%, 45%);"></div></div>
        </div>
        <div class="{{ 'green' if p.status == 'won' else ('red' if p.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
          {% if p.get('hypothetical_pnl') is not none %}${{ "%.2f"|format(p.get('hypothetical_pnl')) }}{% else %}pending{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">No picks yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>All Parlay Tickets</h3>
    <div class="sub" style="margin-bottom:10px;">Every paper parlay ticket, win or lose. Paper-only forever -- Kalshi has no real parlay product.</div>
    {% if all_parlay_tickets %}
      {% for t in all_parlay_tickets[:15] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ t.legs|length }}-leg ticket</strong> <span class="badge">{{ t.status }}</span> <span class="badge badge-bet">{{ "%.0f"|format((t.combined_prob or 0)*100) }}% combined</span>{% if t.get('potential_payout') is not none %} <span class="badge">pays +${{ "%.2f"|format(t.potential_payout) }}</span>{% endif %}</div>
          <div class="sub">{% for l in t.legs %}{{ l.picked_team }} ({{ l.league }}, {{ "%.0f"|format((l.entry_price or 0)*100) }}%){% if not loop.last %}, {% endif %}{% endfor %}</div>
          <div class="sub">staked ${{ "%.2f"|format(t.stake_dollars or 0) }} · {{ t.get('picked_at_fmt') or t.picked_at }}</div>
        </div>
        <div class="{{ 'green' if t.status == 'won' else ('red' if t.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
          {% if t.get('hypothetical_pnl') is not none %}${{ "%.2f"|format(t.get('hypothetical_pnl')) }}{% else %}pending{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">No parlay tickets yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>All Player Prop Tickets</h3>
    <div class="sub" style="margin-bottom:10px;">Every PrizePicks-style paper ticket. Uses sportsbook consensus lines, not PrizePicks' own numbers -- see the Player Props card above for why.</div>
    {% if all_prop_tickets %}
      {% for t in all_prop_tickets[:15] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ t.legs|length }}-leg {{ t.league|upper }} ticket</strong> <span class="badge">{{ t.status }}</span> <span class="badge badge-bet">{{ "%.0f"|format((t.combined_prob or 0)*100) }}% combined</span>{% if t.get('potential_payout') is not none %} <span class="badge">pays +${{ "%.2f"|format(t.potential_payout) }}</span>{% endif %}</div>
          <div class="sub">{% for l in t.legs %}{{ l.player }} {{ l.side|upper }} {{ l.line }} {{ l.stat_type }} ({{ "%.0f"|format((l.consensus_prob or 0)*100) }}%){% if not loop.last %}, {% endif %}{% endfor %}</div>
          <div class="sub">staked ${{ "%.2f"|format(t.stake_dollars or 0) }} · {{ t.get('picked_at_fmt') or t.picked_at }}</div>
        </div>
        <div class="{{ 'green' if t.status == 'won' else ('red' if t.status == 'lost' else 'muted') }}" style="white-space:nowrap;">
          {% if t.get('hypothetical_pnl') is not none %}${{ "%.2f"|format(t.get('hypothetical_pnl')) }}{% elif t.status == 'needs_manual_check' %}check manually{% else %}pending{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">No player prop tickets yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>Pending Picks — Live Countdown</h3>
    <div class="sub" style="margin-bottom:10px;">Everything currently in play, with when it started and when it resolves. <span class="green">🔥 Strong</span> = clears the real-trading bar. <span style="color:#d4a72c;">👍 Thin edge</span> = real edge, smaller/optional. <span class="muted">⚠ Skip</span> = no real edge, data only.</div>
    {% if pending_picks %}
      {% for p in pending_picks %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ p.name }}</strong> <span class="badge">{{ p.category }}</span>{% set _tier = p.get('bet_tier') or ('strong' if p.get('manual_bet_candidate') else 'skip') %}{% if _tier == 'strong' %} <span class="badge badge-bet">🔥 strong -- bet this</span>{% elif _tier == 'thin' %} <span class="badge badge-thin">👍 thin edge -- your call</span>{% else %} <span class="badge badge-data">⚠ skip -- no edge</span>{% endif %}</div>
          <div class="sub">{{ p.opponent }} · contract price ${{ "%.2f"|format(p.entry_price) }} ({{ "%.0f"|format((p.entry_price or 0)*100) }}% implied) · <strong>$15 bet</strong>{% if p.get('potential_payout') is not none %} · pays <strong class="green">+${{ "%.2f"|format(p.potential_payout) }}</strong> if it hits{% endif %}</div>
          <div class="score-bar"><div class="score-bar-fill" style="width:{{ p.get('pick_score', 0) }}%; background:hsl({{ (p.get('pick_score', 0) * 1.2)|round(0, 'floor')|int }}, 70%, 45%);"></div></div>
          <div class="sub">Picked {{ p.picked_at or "recently" }} · {{ p.timing_label }}: {{ p.timing_value or "unknown" }}</div>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">Nothing pending right now.</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>Recent Wins</h3>
    <div class="sub" style="margin-bottom:10px;">Last picks that hit — most recent first.</div>
    {% if recent_wins %}
      {% for p in recent_wins[:10] %}
      <div class="row" style="align-items:flex-start; margin-bottom:8px; border-bottom:1px solid #21262d; padding-bottom:8px;">
        <div>
          <div><strong>{{ p.name }}</strong> <span class="badge">{{ p.category }}</span>{% if p.exit_reason == "early_profit_target" %} <span class="badge">early exit</span>{% endif %}</div>
          <div class="sub">entry ${{ "%.2f"|format(p.entry_price) }}{% if p.exit_price %} → exit ${{ "%.2f"|format(p.exit_price) }}{% endif %} · {{ p.resolved_at }}</div>
        </div>
        <div class="green" style="white-space:nowrap;">${{ "%.2f"|format(p.pnl) }}</div>
      </div>
      {% endfor %}
    {% else %}
      <div class="muted">No wins yet.</div>
    {% endif %}
  </div>

  {% if adaptive.last_adjusted %}
  <div class="muted" style="text-align:center; margin-bottom:8px;">Last time it changed a setting on its own: {{ adaptive.last_adjusted }}</div>
  {% endif %}

  <details>
    <summary>Your real Kalshi account (actual dollars — for reference)</summary>

    <div class="card">
      <div class="row"><span class="label">Money in your Kalshi account</span><span>${{ "%.2f"|format(balance) }}</span></div>
      <div class="sub" style="margin:-6px 0 8px;">This is your TRUE account total, straight from Kalshi -- it already includes any bets you place manually on the Kalshi app yourself, not just the bot's trades.</div>
      <div class="row"><span class="label">How much the bot is allowed to use</span><span>${{ "%.2f"|format(available_budget) }}</span></div>
      <div class="row"><span class="label">Tied up in open real bets right now</span><span>${{ "%.2f"|format(committed) }}</span></div>
      <div class="row"><span class="label">Real profit/loss so far (bot's own bets only, excludes your manual bets)</span>
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

  <div class="muted" style="margin-top:20px; text-align:center;">Picks Engine</div>
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
    parlay_bank = bankroll.get("parlay", {"balance": pt.PAPER_STARTING_BANKROLL})
    props_bank = bankroll.get("props", {"balance": pt.PAPER_STARTING_BANKROLL})
    adaptive = pt.load_adaptive_settings()
    min_sample = pt.MIN_SAMPLE_FOR_ADJUSTMENT

    favorite_current = pt.get_effective_favorite_min_prob()
    favorite_default = pt.MONEYLINE_FAVORITE_MIN_PROB_DEFAULT
    favorite_note = f"{favorite_current*100:.0f}%" + (" (raised)" if favorite_current > favorite_default else "")

    all_moneyline_picks_early = pt.load_paper_trades().get("moneyline", [])
    ml_early_exits = [p for p in all_moneyline_picks_early if p.get("exit_reason") == "early_profit_target"]
    ml_early_exit_note = (
        f"{len(ml_early_exits)} pick(s) cashed out early instead of waiting for the game to finish "
        f"(threshold: {pt.MONEYLINE_PAPER_EARLY_EXIT_PROB*100:.0f}% implied) -- "
        f"${sum(p.get('hypothetical_pnl') or 0 for p in ml_early_exits):+.2f} from those so far. "
        f"This threshold isn't backed by tracked price data yet -- it's a new experiment."
        if pt.MONEYLINE_PAPER_EARLY_EXIT_ENABLED else
        "Disabled (MONEYLINE_PAPER_EARLY_EXIT_ENABLED=false) -- holding every pick until the game finishes"
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
                {
                    "label": "Early profit-taking",
                    "value": f"On, at {pt.MONEYLINE_PAPER_EARLY_EXIT_PROB*100:.0f}% implied" if pt.MONEYLINE_PAPER_EARLY_EXIT_ENABLED else "Off",
                    "explanation": ml_early_exit_note,
                },
                {
                    "label": "Practice-only data collection",
                    "value": f"Also picking down to {max(0.50, favorite_current - 0.03)*100:.0f}% likely to win",
                    "explanation": (
                        "Real trading is off, so there's no real-money cost to also taking a few games just "
                        "below the normal bar above -- purely to build up a bigger sample faster while it's "
                        "safe to. These extra picks don't count toward the bar-raising safety check above them; "
                        "they're just more data."
                    ),
                },
            ],
            "sample": min(adaptive.get("moneyline_sample_size", 0), min_sample),
        },
        {
            "key": "parlay", "label": "Parlay Mode (Experimental, Paper-Only Forever)",
            "summary": summary.get("parlay", {"resolved": 0, "win_rate": None}),
            "bankroll_balance": parlay_bank["balance"],
            "bankroll_down": parlay_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - parlay_bank["balance"]),
            "settings": [
                {
                    "label": "Leg-count combos tried",
                    "value": ", ".join(str(n) for n in sorted(set(pt.PARLAY_LEG_COUNTS))),
                    "explanation": "Builds one ticket per leg count every cycle, reusing the same ranked candidate pool for each (2-leg is the top 2 picks, 3-leg adds the next-best, etc.), so it's clear from real data which size actually pays off -- see the leg-count breakdown below.",
                },
                {
                    "label": "Heaviest favorite allowed per leg",
                    "value": f"${pt.PARLAY_MAX_LEG_PRICE:.2f}",
                    "explanation": "A favorite priced above this eats parlay payout value without adding much safety, so it's skipped for the next-best leg -- a real pattern found in the user's own betting history.",
                },
            ],
            "sample": min(summary.get("parlay", {}).get("resolved", 0), min_sample),
            "note": "Kalshi has no parlay product -- this can never place a real trade, paper-only forever, purely to compare against single-position picks.",
        },
        {
            "key": "props", "label": "PrizePicks-Style Player Props (Experimental, Paper-Only)",
            "summary": summary.get("props", {"resolved": 0, "win_rate": None}),
            "bankroll_balance": props_bank["balance"],
            "bankroll_down": props_bank["balance"] < pt.PAPER_STARTING_BANKROLL,
            "bankroll_down_by": max(0.0, pt.PAPER_STARTING_BANKROLL - props_bank["balance"]),
            "settings": [
                {
                    "label": "Legs per ticket",
                    "value": f"{pt.PROP_LEG_COUNT}",
                    "explanation": "Bundles this many different players' strongest sportsbook-consensus prop picks into one all-or-nothing ticket, PrizePicks Power-Play style.",
                },
                {
                    "label": "Minimum consensus required",
                    "value": f"{pt.PROP_MIN_CONSENSUS_PROB*100:.0f}%",
                    "explanation": "Only takes a side if sportsbooks collectively lean at least this hard toward it -- skips true toss-up props.",
                },
            ],
            "sample": min(summary.get("props", {}).get("resolved", 0), min_sample),
            "note": "Uses sportsbook consensus lines, not PrizePicks' own exact numbers -- PrizePicks has no public API. Only MLB/NBA/WNBA get auto-graded against real box scores (NBA/WNBA unverified); a ticket that can't be confirmed either way shows as \"needs manual check\" instead of a guess.",
        },
    ]

    all_moneyline_picks = pt.load_paper_trades().get("moneyline", [])
    for _p in all_moneyline_picks:
        if _p.get("status") == "pending" and (_p.get("entry_price") or 0) > 0:
            _contracts = max(1.0, pt.PAPER_STAKE_DOLLARS / _p["entry_price"])
            _fee = pt._kalshi_taker_fee_dollars_local(_p["entry_price"], _contracts)
            _p["potential_payout"] = round((1.0 - _p["entry_price"]) * _contracts - _fee, 2)
    too_close_picks = [p for p in all_moneyline_picks if p.get("is_too_close")][-15:][::-1]
    all_recent_picks = list(reversed(sorted(all_moneyline_picks, key=lambda p: p.get("picked_at") or "")))

    all_parlay_tickets = list(reversed(sorted(pt.load_paper_trades().get("parlay", []), key=lambda t: t.get("picked_at") or "")))
    all_prop_tickets = list(reversed(sorted(pt.load_paper_trades().get("props", []), key=lambda t: t.get("picked_at") or "")))

    # Combined "whole slip" hit probability -- parlay already tracks
    # combined_entry_price (product of each leg's Kalshi contract price,
    # which IS the market's implied combined probability); props tickets
    # don't store one, so compute it here as the product of each leg's own
    # sportsbook-consensus probability.
    for _t in all_parlay_tickets:
        _t["combined_prob"] = _t.get("combined_entry_price")
        if _t.get("status") == "pending" and _t.get("contracts") and _t.get("combined_entry_price") is not None:
            _t["potential_payout"] = round((1.0 - _t["combined_entry_price"]) * _t["contracts"] - (_t.get("entry_fees") or 0), 2)
    for _t in all_prop_tickets:
        _cp = 1.0
        for _l in _t.get("legs", []):
            _cp *= (_l.get("consensus_prob") or 0.5)
        _t["combined_prob"] = _cp
        if _t.get("status") == "pending":
            # Flat 3x-stake stand-in payout -- same rough multiplier
            # resolve_prop_paper_trades actually pays out, see its own
            # comment for why this isn't a real PrizePicks-accurate number.
            _t["potential_payout"] = round((_t.get("stake_dollars") or pt.PAPER_STAKE_DOLLARS) * 3, 2)

    # Passing yards -- MAIN FOCUS, at the user's request: its own
    # prominent card near the top of the dashboard (see the template),
    # not buried with the other prop types.
    all_passing_yards_picks = list(reversed(sorted(pt.load_paper_trades().get("passing_yards", []), key=lambda p: p.get("picked_at") or "")))
    for _p in all_passing_yards_picks:
        if _p.get("status") == "pending" and (_p.get("entry_price") or 0) > 0:
            _contracts = max(1.0, pt.PAPER_STAKE_DOLLARS / _p["entry_price"])
            _fee = pt._kalshi_taker_fee_dollars_local(_p["entry_price"], _contracts)
            _p["potential_payout"] = round((1.0 - _p["entry_price"]) * _contracts - _fee, 2)
    passing_yards_summary = summary.get("passing_yards", {"resolved": 0, "win_rate": None, "total_hypothetical_pnl": None, "total_picks": 0})
    passing_yards_bank = bankroll.get("passing_yards", {"balance": pt.PAPER_STARTING_BANKROLL})

    all_wnba_combined_picks = list(reversed(sorted(pt.load_paper_trades().get("wnba_combined", []), key=lambda p: p.get("picked_at") or "")))
    for _p in all_wnba_combined_picks:
        if _p.get("status") == "pending" and (_p.get("entry_price") or 0) > 0:
            _contracts = max(1.0, pt.PAPER_STAKE_DOLLARS / _p["entry_price"])
            _fee = pt._kalshi_taker_fee_dollars_local(_p["entry_price"], _contracts)
            _p["potential_payout"] = round((1.0 - _p["entry_price"]) * _contracts - _fee, 2)
    wnba_combined_summary = summary.get("wnba_combined", {"resolved": 0, "win_rate": None, "total_hypothetical_pnl": None, "total_picks": 0})
    wnba_combined_bank = bankroll.get("wnba_combined", {"balance": pt.PAPER_STARTING_BANKROLL})

    # Profitability status -- same bar (real sample + real profit) the
    # Discord check_profitability_milestones alert uses, shown here too
    # so the answer is visible any time without waiting for a Discord
    # message. moneyline is the only category that could ever place a
    # REAL trade (parlay/props never can -- no Kalshi product).
    _real_capable_status = {"moneyline": bool(worker.REAL_TRADING_LEAGUES)}
    _category_labels = {"moneyline": "Sports Moneyline", "parlay": "Parlay Mode", "props": "Player Props", "passing_yards": "Passing Yards (NFL/NCAAF)"}
    profitability_status = []
    for _cat in ["moneyline", "passing_yards", "parlay", "props"]:
        _stats = summary.get(_cat, {"resolved": 0, "win_rate": None, "total_hypothetical_pnl": None})
        _resolved = _stats.get("resolved", 0)
        _pnl = _stats.get("total_hypothetical_pnl")
        _is_profitable = _resolved >= min_sample and _pnl is not None and _pnl > 0
        profitability_status.append({
            "label": _category_labels[_cat], "resolved": _resolved, "min_sample": min_sample,
            "win_rate": _stats.get("win_rate"), "pnl": _pnl, "is_profitable": _is_profitable,
            "can_go_real": _cat == "moneyline", "real_on": _real_capable_status.get(_cat, False),
        })

    recent_wins = []
    for p in all_moneyline_picks:
        if p.get("status") == "won" and p.get("hypothetical_pnl") is not None:
            recent_wins.append({
                "name": p.get("picked_team"), "category": p.get("league", "?"),
                "entry_price": p.get("entry_price") or 0, "exit_price": p.get("exit_price"),
                "exit_reason": p.get("exit_reason"), "resolved_at": p.get("resolved_at", ""),
                "pnl": p.get("hypothetical_pnl"),
            })
    recent_wins.sort(key=lambda w: w.get("resolved_at") or "", reverse=True)

    cycle_status = safe_read_json(worker.CYCLE_STATUS_FILE, {})
    error_log = safe_read_json(worker.ERROR_LOG_FILE, [])

    def _fmt_ago(iso_str):
        try:
            dt = datetime.fromisoformat(iso_str)
            secs = (datetime.now() - dt).total_seconds()
        except Exception:
            return None
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs/60)}m ago"
        return f"{secs/3600:.1f}h ago"

    def _fmt_when(iso_str):
        """Human date+time for any stored timestamp, e.g. 'Sep 9, 7:46 PM'."""
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt.strftime("%b %-d, %-I:%M %p")
        except Exception:
            return None

    def _fmt_countdown(iso_str):
        """'starts in 2h 15m' / 'in progress' / 'starting any moment' for a
        future (or just-passed) timestamp -- used for game start times and
        a game's start time. Returns None if there's nothing to show."""
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc) if dt.tzinfo is not None else datetime.now()
            secs = (dt - now).total_seconds()
        except Exception:
            return None
        if secs <= -600:
            return "in progress"
        if secs <= 0:
            return "starting any moment"
        if secs < 60:
            return f"in {int(secs)}s"
        if secs < 3600:
            return f"in {int(secs/60)}m"
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        return f"in {h}h {m}m"

    finished_at = cycle_status.get("cycle_finished_at")
    started_at = cycle_status.get("cycle_started_at")
    interval = cycle_status.get("scan_interval_seconds", worker.SCAN_INTERVAL_SECONDS)
    if finished_at and (not started_at or finished_at >= started_at):
        # Idle between cycles -- currently waiting for the next one.
        ago = _fmt_ago(finished_at)
        status_text = f"Idle — finished last scan {ago}" if ago else "Idle"
        try:
            secs_left = interval - (datetime.now() - datetime.fromisoformat(finished_at)).total_seconds()
            next_scan_text = f"in ~{max(0, int(secs_left))}s" if secs_left > 0 else "any moment now"
        except Exception:
            next_scan_text = f"every {interval}s"
    elif started_at:
        ago = _fmt_ago(started_at)
        status_text = f"Scanning now (started {ago})" if ago else "Scanning now"
        next_scan_text = "right after this scan finishes"
    else:
        status_text = "Starting up..."
        next_scan_text = f"every {interval}s"

    recent_errors = list(reversed(error_log[-5:]))
    for err in recent_errors:
        ago = _fmt_ago(err.get("at", ""))
        if ago:
            err["at"] = ago

    # Nicely format Recent Wins' resolved_at now that _fmt_when exists.
    for w in recent_wins:
        w["resolved_at"] = _fmt_when(w.get("resolved_at")) or w.get("resolved_at") or ""

    # Nicely format Close Calls' game-start countdown.
    for p in too_close_picks:
        p["starts_in"] = _fmt_countdown(p.get("event_start_time"))

    # Pending Picks -- every still-open moneyline pick, with when it
    # started/starts and when it resolves, so there's one place to see
    # "what's live right now and when do I find out."
    pending_picks = []
    for p in all_moneyline_picks:
        if p.get("status") != "pending":
            continue
        if p.get("source") == "pregame":
            timing_label, timing_value = "Game starts", _fmt_countdown(p.get("event_start_time"))
        else:
            timing_label, timing_value = "Status", "in progress (live pick)"
        _price = p.get("entry_price") or 0
        _payout = None
        if _price > 0:
            _contracts = max(1.0, pt.PAPER_STAKE_DOLLARS / _price)
            _fee = pt._kalshi_taker_fee_dollars_local(_price, _contracts)
            _payout = round((1.0 - _price) * _contracts - _fee, 2)
        pending_picks.append({
            "name": p.get("picked_team"), "category": p.get("league", "?"),
            "opponent": f"{p.get('away_team')} @ {p.get('home_team')}",
            "picked_at": _fmt_when(p.get("picked_at")),
            "timing_label": timing_label, "timing_value": timing_value,
            "entry_price": p.get("entry_price") or 0,
            "manual_bet_candidate": p.get("manual_bet_candidate"),
            "bet_tier": p.get("bet_tier"),
            "pick_score": p.get("pick_score") or 0,
            "potential_payout": _payout,
        })

    activity = {
        "status_text": status_text,
        "next_scan_text": next_scan_text,
        "recent_errors": recent_errors,
        "error_window_label": "recent scans",
    }

    real_trading_on = bool(worker.REAL_TRADING_LEAGUES)
    real_trading_summary = ", ".join(sorted(worker.REAL_TRADING_LEAGUES)) if worker.REAL_TRADING_LEAGUES else ""

    trading_halted = ledger.is_trading_halted()
    bot_pnl_data = ledger.load_bot_pnl()
    current_loss_pct = ledger.get_realized_loss_pct()

    # Performance chart -- PrizePicks-style running P&L across every
    # resolved paper pick, every category combined, oldest to newest.
    all_trades_data = pt.load_paper_trades()
    resolved_events = []
    for p in all_trades_data.get("moneyline", []):
        if p.get("status") in ("won", "lost") and p.get("resolved_at"):
            resolved_events.append((p["resolved_at"], p.get("hypothetical_pnl") or 0.0))
    for p in all_trades_data.get("passing_yards", []):
        if p.get("status") in ("won", "lost") and p.get("resolved_at"):
            resolved_events.append((p["resolved_at"], p.get("hypothetical_pnl") or 0.0))
    for p in all_trades_data.get("wnba_combined", []):
        if p.get("status") in ("won", "lost") and p.get("resolved_at"):
            resolved_events.append((p["resolved_at"], p.get("hypothetical_pnl") or 0.0))
    for t in all_trades_data.get("parlay", []):
        if t.get("status") in ("won", "lost") and t.get("resolved_at"):
            resolved_events.append((t["resolved_at"], t.get("hypothetical_pnl") or 0.0))
    for t in all_trades_data.get("props", []):
        if t.get("status") in ("won", "lost") and t.get("resolved_at"):
            resolved_events.append((t["resolved_at"], t.get("hypothetical_pnl") or 0.0))
    resolved_events.sort(key=lambda e: e[0])

    running = 0.0
    pnl_points = [0.0]
    for _, pnl in resolved_events:
        running += pnl
        pnl_points.append(round(running, 2))
    chart_svg = _build_pnl_chart_svg(pnl_points)
    total_resolved_count = len(resolved_events)
    net_pnl = pnl_points[-1] if pnl_points else 0.0

    parlay_leg_breakdown = pt.get_parlay_leg_count_breakdown()

    manual_bet_summary = pt.get_manual_bet_summary()
    manual_bets_list = pt.get_manual_bets_with_status()[:20]
    loggable_picks = pt.get_loggable_picks()

    return render_template_string(
        PAGE_TEMPLATE,
        manual_bet_summary=manual_bet_summary,
        manual_bets_list=manual_bets_list,
        loggable_picks=loggable_picks,
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
        recent_wins=recent_wins,
        pending_picks=pending_picks,
        all_recent_picks=all_recent_picks,
        all_parlay_tickets=all_parlay_tickets,
        all_prop_tickets=all_prop_tickets,
        all_passing_yards_picks=all_passing_yards_picks,
        passing_yards_summary=passing_yards_summary,
        all_wnba_combined_picks=all_wnba_combined_picks,
        wnba_combined_summary=wnba_combined_summary,
        wnba_combined_bank=wnba_combined_bank,
        passing_yards_bank=passing_yards_bank,
        profitability_status=profitability_status,
        real_trading_on=real_trading_on,
        real_trading_summary=real_trading_summary,
        trading_halted=trading_halted,
        halted_reason=bot_pnl_data.get("halted_reason"),
        loss_limit_percent=ledger.MAX_LOSS_PERCENT,
        current_loss_pct=current_loss_pct,
        activity=activity,
        chart_svg=chart_svg,
        total_resolved_count=total_resolved_count,
        net_pnl=net_pnl,
        parlay_leg_breakdown=parlay_leg_breakdown,
    )


@app.route("/log_manual_bet", methods=["POST"])
def log_manual_bet():
    """Logs a real bet the user placed themselves against one of the bot's
    picks, so manual-betting performance can be tracked over time -- never
    places anything, purely a record."""
    import paper_trading as pt
    pick_id = (request.form.get("pick_id") or "").strip()
    stake_raw = request.form.get("stake_dollars") or "0"
    note = (request.form.get("note") or "").strip() or None
    try:
        stake_dollars = float(stake_raw)
    except ValueError:
        stake_dollars = 0.0
    if pick_id and stake_dollars > 0:
        pt.record_manual_bet(pick_id, stake_dollars, side_note=note)
    return redirect("/")


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
    Only logged for sports/moneyline trades (parlay/props don't have a "fair prob"
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
    data = load_json("paper_trades.json", {"moneyline": []})
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


@app.route("/purge_duplicate_live_picks", methods=["POST"])
def purge_duplicate_live_picks_route():
    """One-time cleanup for a real bug found 2026-09-09: SharpAPI can hand
    the same real-world live match two different event_ids across scans
    (confirmed: an ATP match showed up as both "..._b2" and "..._b3"),
    and the old dedup logic in live_trading.track_live_candidates keyed
    only on event_id, so it tracked and picked the SAME real match twice.
    Fixed going forward (dedup now also checks the matched Kalshi ticker
    pair) -- this route removes the duplicate picks that already happened
    before the fix, keeping the earliest one of each (league, away_ticker,
    home_ticker, side) group so real learning stats aren't double-counted.
    No dashboard button -- trigger with:
    curl -X POST https://<your-app>.up.railway.app/purge_duplicate_live_picks
    """
    import paper_trading as pt
    data = pt.load_paper_trades()
    picks = data.get("moneyline", [])

    seen = {}
    kept, removed = [], 0
    for p in sorted(picks, key=lambda p: p.get("picked_at") or ""):
        dedup_key = (p.get("league"), p.get("kalshi_ticker"), p.get("side"))
        if p.get("source") == "live" and dedup_key in seen:
            removed += 1
            continue
        if p.get("source") == "live":
            seen[dedup_key] = True
        kept.append(p)

    data["moneyline"] = kept
    pt.save_paper_trades(data)
    return f"Kept {len(kept)} picks, removed {removed} duplicate live pick(s).\n"


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
    worker.flush_discord_queue(force=True)  # manual trigger -- send any picks right away, don't wait for the interval gate

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


@app.route("/health")
def health_route():
    """Machine-readable health check for automated monitoring -- the same
    facts the "/" dashboard shows as human-readable text (activity.status_text,
    recent_errors), but as raw JSON so a script doesn't have to scrape HTML.
    """
    import worker
    cycle_status = safe_read_json(worker.CYCLE_STATUS_FILE, {})
    error_log = safe_read_json(worker.ERROR_LOG_FILE, [])

    finished_at = cycle_status.get("cycle_finished_at")
    started_at = cycle_status.get("cycle_started_at")
    interval = cycle_status.get("scan_interval_seconds", worker.SCAN_INTERVAL_SECONDS)

    seconds_since_last_finish = None
    if finished_at:
        try:
            seconds_since_last_finish = (
                datetime.now() - datetime.fromisoformat(finished_at)
            ).total_seconds()
        except Exception:
            seconds_since_last_finish = None

    if finished_at and (not started_at or finished_at >= started_at):
        cycle_state = "idle"
    elif started_at:
        cycle_state = "scanning"
    else:
        cycle_state = "starting_up"

    # Stale if we've gone more than 2x the scan interval since the last
    # completed cycle with no new cycle in progress -- a scan should never
    # take that long to come back around under normal operation.
    stale = (
        cycle_state == "idle"
        and seconds_since_last_finish is not None
        and seconds_since_last_finish > 2 * interval
    )

    ledger_data = ledger.load_bot_pnl()

    return {
        "halted": ledger_data.get("halted", False),
        "halted_reason": ledger_data.get("halted_reason"),
        "halted_at": ledger_data.get("halted_at"),
        "cycle_state": cycle_state,
        "cycle_started_at": started_at,
        "cycle_finished_at": finished_at,
        "seconds_since_last_scan": seconds_since_last_finish,
        "scan_interval_seconds": interval,
        "stale": stale,
        "recent_errors": list(reversed(error_log[-5:])),
        "error_count_total": len(error_log),
    }


@app.route("/resume_trading", methods=["POST"])
def resume_trading_route():
    """Manually clears the drawdown circuit breaker's halt so real trading
    (sports, if you've separately turned real trading on) can resume. This
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
            seen.setdefault(eid, []).append({
                "away_team": away, "home_team": home,
                "event_start_time": row.get("event_start_time"),
                "sportsbook": row.get("sportsbook"), "market_type": row.get("market_type"),
                "is_main_line": row.get("is_main_line"), "selection": row.get("selection"),
            })
    return {"league": league, "team": team, "total_rows_scanned": len(rows), "matches": seen}


@app.route("/sharpapi_fetch_health")
def sharpapi_fetch_health_route():
    """
    Read-only: shows the recorded history of every fetch_sharpapi_odds
    call (complete or not), WITHOUT triggering a new fetch itself -- so
    this can be checked repeatedly to see how the bot's own unassisted
    scan cycles are actually doing, without adding to the rate-limit
    load being investigated. ?league=mlb to filter, ?since_minutes=30
    to only show recent entries.
    """
    import worker
    history = safe_read_json(worker.SHARPAPI_FETCH_HEALTH_FILE, [])
    league = request.args.get("league", "").lower()
    if league:
        history = [h for h in history if h.get("league") == league]
    since_minutes = request.args.get("since_minutes")
    if since_minutes:
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=float(since_minutes))
        history = [h for h in history if h.get("at") and datetime.fromisoformat(h["at"]) >= cutoff]
    incomplete = [h for h in history if not h.get("complete")]
    return {
        "total_entries": len(history),
        "incomplete_count": len(incomplete),
        "incomplete_entries": incomplete,
        "recent_entries": history[-20:],
    }


@app.route("/debug/props_sample")
def debug_props_sample_route():
    """
    Read-only: shows the most recent raw SharpAPI player-prop row
    fetch_sharpapi_player_props saw (or a note that a fetch returned zero
    rows), written by worker.py the first time each cycle it gets a
    result. Exists to verify the player-prop field-name guesses in
    paper_trading._parse_player_prop_row without needing Discord access --
    see that function's docstring for why the exact schema wasn't
    confirmed before deploying.
    """
    sample = load_json("props_debug_sample.json", {})
    if not sample:
        return {"status": "no data yet -- no player-prop fetch has completed since this route was added"}
    return sample


@app.route("/debug/props_probe")
def debug_props_probe_route():
    """
    Read-only: shows the one-time player-prop market-parameter probe
    (see worker.probe_sharpapi_player_prop_market) -- raw status/body for
    each candidate market value tried against the real SharpAPI key, so
    the correct value can be confirmed from real data.
    """
    probe = load_json("props_market_probe.json", None)
    if probe is None:
        return {"status": "no probe result yet -- runs once at the start of the next sports scan cycle"}
    return probe


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
