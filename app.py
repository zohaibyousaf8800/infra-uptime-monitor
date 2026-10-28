"""
app.py — Flask web app for infra-uptime-monitor.

Serves a server-rendered status page at / and a small JSON API under /api/.
Running `python app.py` initialises the SQLite database, loads targets from
config.json, starts the background checker thread, and serves HTTP on
$PORT (default 5001).
"""

import os
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request

import monitor as mon

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "uptime.db"
DEFAULT_CONFIG = BASE_DIR / "config.json"

STATUS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Infra Uptime Monitor</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem auto; max-width: 960px; padding: 0 1rem; color: #1c2733; }
  h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
  p.sub { color: #5a6b7a; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { text-align: left; padding: 0.55rem 0.75rem; border-bottom: 1px solid #dde4ea; }
  th { background: #f2f5f8; font-weight: 600; }
  .dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
         margin-right: 0.5rem; vertical-align: baseline; }
  .up { background: #1faa55; } .down { background: #d9382b; } .unknown { background: #9aa7b1; }
  .open-incident { background: #fdf0ef; border: 1px solid #f0c6c2; border-radius: 6px;
                   padding: 0.6rem 0.9rem; margin: 0.5rem 0; }
  .ok-box { background: #eef8f1; border: 1px solid #c4e6cf; border-radius: 6px;
            padding: 0.6rem 0.9rem; margin: 0.5rem 0; }
  a { color: #0b5cad; text-decoration: none; }
  footer { color: #8a99a6; font-size: 0.85rem; margin-top: 2rem; }
</style>
</head>
<body>
<h1>Infra Uptime Monitor</h1>
<p class="sub">{{ summary.targets_up }} of {{ summary.targets_total }} targets up &middot;
   overall uptime (24 h): {{ summary.overall_uptime_24h }}% &middot;
   {{ summary.total_checks_24h }} checks in the last 24 h</p>

<table>
  <thead>
    <tr><th>Target</th><th>URL</th><th>Uptime 24 h</th><th>Last response</th><th>Last checked</th></tr>
  </thead>
  <tbody>
  {% for t in targets %}
    <tr>
      <td><span class="dot {{ t.status }}"></span>{{ t.name }}</td>
      <td><a href="{{ t.url }}">{{ t.url }}</a></td>
      <td>{{ t.uptime_24h }}%</td>
      <td>{% if t.last_response_ms is not none %}{{ t.last_response_ms }} ms{% else %}&mdash;{% endif %}</td>
      <td>{{ t.last_checked_at or "&mdash;" | safe }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<h2>Open incidents</h2>
{% if open_incidents %}
  {% for inc in open_incidents %}
    <div class="open-incident"><strong>{{ inc.target_name }}</strong> &mdash; down since {{ inc.started_at }}</div>
  {% endfor %}
{% else %}
  <div class="ok-box">No open incidents.</div>
{% endif %}

<footer>Auto-refreshes every 30 seconds. Data from the local monitoring database.</footer>
</body>
</html>
"""


def create_app(db_path=DEFAULT_DB, config_path=DEFAULT_CONFIG, start_checker=False):
    app = Flask(__name__)
    db_path = Path(db_path)
    monitor = mon.bootstrap(db_path, config_path)
    if start_checker:
        monitor.start()
    app.extensions["monitor"] = monitor

    def targets_with_status():
        return [mon.target_status(db_path, t) for t in mon.list_targets(db_path)]

    @app.get("/")
    def status_page():
        incidents = mon.list_incidents(db_path)
        return render_template_string(
            STATUS_PAGE,
            targets=targets_with_status(),
            summary=mon.summary(db_path),
            open_incidents=[i for i in incidents if i["ended_at"] is None],
        )

    @app.get("/api/targets")
    def api_targets():
        return jsonify(targets_with_status())

    @app.get("/api/targets/<int:target_id>/history")
    def api_history(target_id):
        known = {t["id"] for t in mon.list_targets(db_path)}
        if target_id not in known:
            abort(404)
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 500))
        return jsonify(mon.check_history(db_path, target_id, limit))

    @app.get("/api/incidents")
    def api_incidents():
        incidents = mon.list_incidents(db_path)
        for inc in incidents:
            inc["open"] = inc["ended_at"] is None
        return jsonify(incidents)

    @app.get("/api/summary")
    def api_summary():
        return jsonify(mon.summary(db_path))

    return app


def main():
    port = int(os.environ.get("PORT", "5001"))
    app = create_app(start_checker=True)
    print(f"infra-uptime-monitor listening on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
