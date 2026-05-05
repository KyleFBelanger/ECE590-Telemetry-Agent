"""
Dashboard — simple web UI for the telemetry system.
Shows node health scores and per-epoch metrics from the database.

Run inside the dashboard container:
  python3 dashboard/app.py

Then open http://localhost:5050 in your browser.
"""

import sqlite3
import time
from flask import Flask, render_template_string

app = Flask(__name__)
DB_PATH = '/workspace/data/metrics.db'

# ── database helpers ──────────────────────────────────────────────────────────

def get_health_scores():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT node_id, current_score, last_updated, total_jobs
        FROM health_scores
        ORDER BY current_score DESC
    """).fetchall()
    conn.close()
    return [{"node": r[0], "score": round(r[1], 3),
             "updated": time.strftime('%H:%M:%S', time.localtime(r[2])),
             "jobs": r[3]} for r in rows]


def get_metrics():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT node_id, epoch, all_reduce_ms, rtt_ms, nic_bytes_sent, nic_bytes_recv
        FROM metrics
        ORDER BY epoch, node_id
    """).fetchall()
    conn.close()
    return [{"node": r[0], "epoch": r[1],
             "all_reduce_ms": round(r[2], 2) if r[2] else None,
             "rtt_ms": round(r[3], 2) if r[3] else "timeout",
             "nic_sent_mb": round(r[4] / 1e6, 2) if r[4] else 0,
             "nic_recv_mb": round(r[5] / 1e6, 2) if r[5] else 0}
            for r in rows]


def get_job_ids():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT job_id FROM metrics"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── HTML template ─────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <title>Node Health Dashboard</title>
  <meta http-equiv="refresh" content="10">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 4px; color: #ffffff; }
    .subtitle { font-size: 0.8rem; color: #666; margin-bottom: 24px; }
    .subtitle span { color: #4ade80; }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }

    .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 20px; }
    .card .node-name { font-size: 0.85rem; color: #888; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
    .card .score { font-size: 2.2rem; font-weight: 700; margin-bottom: 10px; }
    .card .score.good  { color: #4ade80; }
    .card .score.ok    { color: #facc15; }
    .card .score.bad   { color: #f87171; }
    .bar-bg { background: #2a2d3a; border-radius: 4px; height: 6px; margin-bottom: 10px; }
    .bar-fill { height: 6px; border-radius: 4px; }
    .bar-fill.good  { background: #4ade80; }
    .bar-fill.ok    { background: #facc15; }
    .bar-fill.bad   { background: #f87171; }
    .card .meta { font-size: 0.75rem; color: #555; }

    h2 { font-size: 1rem; font-weight: 600; margin-bottom: 12px; color: #ccc; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; padding: 8px 12px; background: #1a1d27; color: #666; font-weight: 500;
         border-bottom: 1px solid #2a2d3a; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.05em; }
    td { padding: 8px 12px; border-bottom: 1px solid #1e2130; }
    tr:hover td { background: #1a1d27; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.72rem; font-weight: 600; }
    .tag.good  { background: #14532d; color: #4ade80; }
    .tag.ok    { background: #713f12; color: #facc15; }
    .tag.bad   { background: #450a0a; color: #f87171; }
    .timeout   { color: #f87171; font-style: italic; }
    .section   { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 20px; margin-bottom: 24px; }
    .jobs-row  { font-size: 0.78rem; color: #555; margin-bottom: 16px; }
    .jobs-row span { color: #888; margin-right: 12px; }
    .no-data   { color: #444; font-style: italic; padding: 20px 0; text-align: center; }
  </style>
</head>
<body>

<h1>Node Health Dashboard</h1>
<p class="subtitle">Auto-refreshes every 10s &nbsp;|&nbsp; Jobs tracked: <span>{{ job_ids|length }}</span> &nbsp;|&nbsp; {{ job_ids|join(', ') if job_ids else 'none yet' }}</p>

<!-- Health score cards -->
<div class="grid">
  {% for h in health %}
    {% if h.score >= 0.75 %}{% set cls = 'good' %}
    {% elif h.score >= 0.5  %}{% set cls = 'ok'   %}
    {% else                  %}{% set cls = 'bad'  %}{% endif %}
    <div class="card">
      <div class="node-name">{{ h.node }}</div>
      <div class="score {{ cls }}">{{ h.score }}</div>
      <div class="bar-bg"><div class="bar-fill {{ cls }}" style="width:{{ (h.score * 100)|int }}%"></div></div>
      <div class="meta">{{ h.jobs }} job{{ 's' if h.jobs != 1 }} &nbsp;·&nbsp; updated {{ h.updated }}</div>
    </div>
  {% else %}
    <div class="card"><div class="no-data">No scores yet — run health_scores.py</div></div>
  {% endfor %}
</div>

<!-- Per-epoch metrics table -->
<div class="section">
  <h2>Per-Epoch Metrics</h2>
  {% if metrics %}
  <table>
    <thead>
      <tr>
        <th>Node</th>
        <th>Epoch</th>
        <th>All-Reduce (ms)</th>
        <th>RTT (ms)</th>
        <th>NIC Sent (MB/s)</th>
        <th>NIC Recv (MB/s)</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {% for m in metrics %}
        {% if m.rtt_ms == 'timeout' %}{% set status = 'bad' %}{% set status_label = 'timeout' %}
        {% elif m.rtt_ms and m.rtt_ms > 10 %}{% set status = 'ok' %}{% set status_label = 'slow' %}
        {% else %}{% set status = 'good' %}{% set status_label = 'healthy' %}{% endif %}
        <tr>
          <td><strong>{{ m.node }}</strong></td>
          <td>{{ m.epoch }}</td>
          <td>{{ m.all_reduce_ms }}</td>
          <td {% if m.rtt_ms == 'timeout' %}class="timeout"{% endif %}>{{ m.rtt_ms }}</td>
          <td>{{ m.nic_sent_mb }}</td>
          <td>{{ m.nic_recv_mb }}</td>
          <td><span class="tag {{ status }}">{{ status_label }}</span></td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
    <div class="no-data">No metrics yet — run a training job first</div>
  {% endif %}
</div>

</body>
</html>
"""

# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(
        TEMPLATE,
        health=get_health_scores(),
        metrics=get_metrics(),
        job_ids=get_job_ids()
    )


if __name__ == '__main__':
    print("Dashboard running at http://localhost:5050")
    app.run(host='0.0.0.0', port=5000, debug=False)