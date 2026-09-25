"""Generate the SidelineWire Grafana dashboards as ConfigMaps (k8s/monitoring/dashboards.yaml).

    python k8s/monitoring/dashboards.py && kubectl apply -f k8s/monitoring/dashboards.yaml

Grafana's dashboard sidecar loads every ConfigMap labeled grafana_dashboard=1 into the "SidelineWire" folder.
Edit the panels here, not in the Grafana UI (UI edits to provisioned dashboards aren't saved back).
Data sources: Prometheus (uid prometheus), Loki (uid loki), and the two app databases (pg-prod, pg-dev; read-only).
"""
import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
PG = {"type": "grafana-postgresql-datasource", "uid": "${ds}"}
FOLDER = "SidelineWire"


# --- building blocks -----------------------------------------------------------------------------------------------

def prom(expr, legend="", instant=False, fmt=None):
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        t.update(instant=True, range=False)
    if fmt:
        t["format"] = fmt
    return t


def sql(q, fmt="table"):
    return {"datasource": PG, "rawSql": q.strip(), "format": fmt, "rawQuery": True, "editorMode": "code", "refId": "A"}


def loki(expr):
    return {"datasource": LOKI, "expr": expr, "refId": "A", "queryType": "range"}


def panel(kind, title, targets, w=12, h=8, unit=None, desc=None, **extra):
    targets = targets if isinstance(targets, list) else [targets]
    for i, t in enumerate(targets):
        t["refId"] = chr(65 + i)
    p = {"type": kind, "title": title, "targets": targets, "datasource": targets[0]["datasource"],
         "gridPos": {"w": w, "h": h}, "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if desc:
        p["description"] = desc
    for k, v in extra.items():
        if k == "defaults":
            p["fieldConfig"]["defaults"].update(v)
        elif k == "overrides":
            p["fieldConfig"]["overrides"] = v
        else:
            p[k] = v
    return p


def stat(title, target, unit=None, w=4, h=4, thresholds=None, desc=None, decimals=None):
    defaults = {"color": {"mode": "thresholds"}}
    # without explicit thresholds, stay neutral (Grafana's default turns anything over 80 red)
    steps = thresholds or [(None, "green")]
    defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}
    if decimals is not None:
        defaults["decimals"] = decimals
    return panel("stat", title, target, w=w, h=h, unit=unit, desc=desc, defaults=defaults,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": "value", "graphMode": "area", "textMode": "auto"})


def ts(title, targets, unit=None, w=12, h=8, stack=False, bars=False, desc=None):
    custom = {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never", "spanNulls": True}
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    defaults = {"custom": custom}
    if bars:
        custom.update(drawStyle="bars", fillOpacity=80, lineWidth=1)
        defaults["min"] = 0
    return panel("timeseries", title, targets, w=w, h=h, unit=unit, desc=desc, defaults=defaults,
                 options={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})


def table(title, target, w=12, h=8, desc=None, overrides=None, sort=None):
    opts = {"showHeader": True, "cellHeight": "sm"}
    if sort:
        opts["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return panel("table", title, target, w=w, h=h, desc=desc, options=opts, overrides=overrides or [])


def logs(title, expr, w=24, h=10, desc=None):
    return panel("logs", title, loki(expr), w=w, h=h, desc=desc,
                 options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True})


def row(title):
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"w": 24, "h": 1}, "panels": []}


def layout(panels):
    """Flow panels left to right, wrapping at 24 columns."""
    x = y = rowh = 0
    for i, p in enumerate(panels, start=1):
        w, h = p["gridPos"]["w"], p["gridPos"]["h"]
        if x + w > 24 or p["type"] == "row":
            x, y, rowh = 0, y + rowh, 0
        p["gridPos"].update(x=x, y=y)
        p["id"] = i
        x += w
        rowh = max(rowh, h)
        if p["type"] == "row":
            x, y, rowh = 0, y + h, 0
    return panels


def var_custom(name, label, options, default):
    return {"type": "custom", "name": name, "label": label, "query": ",".join(options),
            "current": {"text": default, "value": default},
            "options": [{"text": o, "value": o, "selected": o == default} for o in options]}


def var_ds(default="pg-prod"):
    return {"type": "datasource", "name": "ds", "label": "Environment", "query": "grafana-postgresql-datasource",
            "regex": "/SidelineWire/", "current": {"text": "SidelineWire prod DB", "value": default}}


def var_text(name, label, default):
    return {"type": "textbox", "name": name, "label": label, "query": default, "current": {"text": default, "value": default}}


def dashboard(uid, title, panels, variables=(), time_from="now-24h", refresh="1m", desc=""):
    return {"uid": uid, "title": title, "description": desc, "tags": ["sidelinewire"], "timezone": "America/New_York",
            "schemaVersion": 39, "version": 1, "editable": True, "graphTooltip": 1, "refresh": refresh,
            "time": {"from": time_from, "to": "now"}, "templating": {"list": list(variables)},
            "annotations": {"list": []}, "panels": layout(panels)}


ENV = var_custom("env", "Environment", ["prod", "dev"], "prod")
NS_VAR = {"type": "custom", "name": "ns", "label": "Environment", "query": "prod : sidelinewire,dev : football-analyst",
          "current": {"text": "prod", "value": "sidelinewire"},
          "options": [{"text": "prod", "value": "sidelinewire", "selected": True},
                      {"text": "dev", "value": "football-analyst", "selected": False}]}


# --- 1. Site & API -------------------------------------------------------------------------------------------------

def site_api():
    e = 'env="$env"'
    rate_all = f'sum(rate(sidelinewire_http_requests_total{{{e}}}[5m]))'
    return dashboard("sw-site", "SidelineWire · Site & API", [
        stat("Requests / min", prom(f"{rate_all} * 60"), unit="reqpm", decimals=0),
        stat("Server errors (5xx)", prom(f'sum(rate(sidelinewire_http_requests_total{{{e},status=~"5.."}}[5m])) / {rate_all}'),
             unit="percentunit", thresholds=[(None, "green"), (0.01, "orange"), (0.05, "red")], decimals=2),
        stat("p95 response time", prom(f'histogram_quantile(0.95, sum by (le) (rate(sidelinewire_http_request_duration_seconds_bucket{{{e}}}[5m])))'),
             unit="s", thresholds=[(None, "green"), (0.5, "orange"), (1.5, "red")], decimals=2),
        stat("API requests / min", prom(f'sum(rate(sidelinewire_api_requests_total{{{e}}}[5m])) * 60'), unit="reqpm", decimals=1),
        stat("API rejected (401/429) / hr", prom(f'sum(increase(sidelinewire_api_requests_total{{{e},status=~"401|429"}}[1h]))'),
             decimals=0, thresholds=[(None, "green"), (50, "orange")]),
        stat("Web pods up", prom(f'sum(up{{{e},job=~".*sidelinewire-web.*"}})'), thresholds=[(None, "red"), (1, "green")]),
        ts("Requests by status", prom(f'sum by (status) (rate(sidelinewire_http_requests_total{{{e}}}[5m])) * 60', "{{status}}"),
           unit="reqpm", stack=True),
        ts("Response time", [prom(f'histogram_quantile(0.5, sum by (le) (rate(sidelinewire_http_request_duration_seconds_bucket{{{e}}}[5m])))', "p50"),
                             prom(f'histogram_quantile(0.95, sum by (le) (rate(sidelinewire_http_request_duration_seconds_bucket{{{e}}}[5m])))', "p95"),
                             prom(f'histogram_quantile(0.99, sum by (le) (rate(sidelinewire_http_request_duration_seconds_bucket{{{e}}}[5m])))', "p99")],
           unit="s"),
        table("Busiest pages (selected range)", prom(f'sort_desc(sum by (route) (increase(sidelinewire_http_requests_total{{{e}}}[$__range])))',
                                                      instant=True, fmt="table"),
              desc="Requests per route over the dashboard's time range.", sort=("Value", True),
              overrides=[{"matcher": {"id": "byName", "options": "Time"}, "properties": [{"id": "custom.hidden", "value": True}]}]),
        table("Slowest pages (p95, selected range)",
              prom(f'sort_desc(histogram_quantile(0.95, sum by (route, le) (rate(sidelinewire_http_request_duration_seconds_bucket{{{e}}}[$__range]))))',
                   instant=True, fmt="table"), sort=("Value", True),
              overrides=[{"matcher": {"id": "byName", "options": "Value"}, "properties": [{"id": "unit", "value": "s"}]},
                         {"matcher": {"id": "byName", "options": "Time"}, "properties": [{"id": "custom.hidden", "value": True}]}]),
        ts("API requests by key", prom(f'sum by (key) (rate(sidelinewire_api_requests_total{{{e}}}[5m])) * 60', "{{key}}"), unit="reqpm"),
        ts("Errors by page (4xx/5xx)", prom(f'sum by (route, status) (rate(sidelinewire_http_requests_total{{{e},status=~"[45].."}}[5m])) * 60',
                                            "{{status}} {{route}}"), unit="reqpm"),
        logs("Web app errors", '{namespace="$ns", app="web"} |~ "(?i)traceback|error|exception"', h=10),
    ], [ENV, dict(NS_VAR, hide=2, query="sidelinewire", current={"text": "sidelinewire", "value": "sidelinewire"})],
        desc="Traffic, latency and errors for sidelinewire.com and the JSON API (metrics.py).")


# --- 2. Pipeline & jobs ------------------------------------------------------------------------------------------

def pipeline():
    n = 'namespace="$ns"'
    return dashboard("sw-pipeline", "SidelineWire · Pipeline & jobs", [
        stat("Failed jobs (kept history)", prom(f'count(kube_job_status_failed{{{n}}} > 0) or vector(0)'),
             thresholds=[(None, "green"), (1, "orange"), (3, "red")],
             desc="Failed Job objects still in history (each CronJob keeps its last 3 failures)."),
        stat("Running now", prom(f'count(kube_job_status_active{{{n}}} > 0) or vector(0)')),
        stat("Minutes since last good refresh",
             prom(f'(time() - max(kube_cronjob_status_last_successful_time{{{n},cronjob=~"refresh-.*"}})) / 60'),
             unit="m", decimals=0, thresholds=[(None, "green"), (90, "orange"), (180, "red")]),
        stat("Minutes since last daily run", prom(f'(time() - kube_cronjob_status_last_successful_time{{{n},cronjob="daily"}}) / 60'),
             unit="m", decimals=0, thresholds=[(None, "green"), (1560, "red")]),
        stat("Suspended CronJobs", prom(f'sum(kube_cronjob_spec_suspend{{{n}}})'), thresholds=[(None, "green"), (1, "blue")]),
        stat("Pipeline pod restarts (24h)", prom(f'sum(increase(kube_pod_container_status_restarts_total{{{n}}}[24h]))'),
             decimals=0, thresholds=[(None, "green"), (1, "orange")]),
        table("Scheduled jobs: last success", prom(f'(time() - kube_cronjob_status_last_successful_time{{{n}}}) / 60', instant=True, fmt="table"),
              w=12, h=9, sort=("Value", True),
              overrides=[{"matcher": {"id": "byName", "options": "Value"}, "properties": [{"id": "displayName", "value": "minutes ago"}, {"id": "decimals", "value": 0}]},
                         {"matcher": {"id": "byRegexp", "options": "^(Time|__name__|container|endpoint|instance|job|namespace|pod|service|uid)$"},
                          "properties": [{"id": "custom.hidden", "value": True}]}]),
        table("Recent job runs (duration, minutes)",
              prom(f'(kube_job_status_completion_time{{{n}}} - kube_job_status_start_time{{{n}}}) / 60', instant=True, fmt="table"),
              w=12, h=9, sort=("job_name", True),
              overrides=[{"matcher": {"id": "byName", "options": "Value"}, "properties": [{"id": "displayName", "value": "minutes"}, {"id": "decimals", "value": 1}]},
                         {"matcher": {"id": "byRegexp", "options": "^(Time|__name__|container|endpoint|instance|job|namespace|pod|service|uid)$"},
                          "properties": [{"id": "custom.hidden", "value": True}]}]),
        ts("Job durations over time", prom(f'max by (cronjob) (label_replace((kube_job_status_completion_time{{{n}}} - kube_job_status_start_time{{{n}}}) / 60, "cronjob", "$1", "job_name", "(.+)-[0-9]+"))',
                                            "{{cronjob}}"), unit="m", w=24),
        logs("Pipeline failures and tracebacks", '{namespace="$ns", app="pipeline"} |~ "FAILED|Traceback|(?i)error"', h=9),
        logs("Pipeline step timings", '{namespace="$ns", app="pipeline"} |= "<<<"', h=9),
    ], [NS_VAR], time_from="now-24h", desc="Scheduled pipeline runs (refresh/daily/weekly) and their logs.")


# --- 3. Live games -------------------------------------------------------------------------------------------------

def live():
    return dashboard("sw-live", "SidelineWire · Live games", [
        stat("Games in progress", sql("SELECT count(*) AS value FROM live_games WHERE state = 'in'")),
        stat("Seconds since last score update", sql("SELECT extract(epoch FROM now() - max(updated_at)) AS value FROM live_games"),
             unit="s", decimals=0, thresholds=[(None, "green"), (120, "orange"), (600, "red")],
             desc="Only meaningful while games are on; the poller slows down between game windows."),
        stat("Seconds since last play (live games)",
             sql("""SELECT extract(epoch FROM now() - max(lg.updated_at)) AS value FROM live_games lg WHERE lg.state = 'in'"""),
             unit="s", decimals=0, thresholds=[(None, "green"), (120, "orange"), (300, "red")]),
        stat("Box scores saved today", sql("SELECT count(*) AS value FROM game_boxscores WHERE updated_at > date_trunc('day', now())")),
        stat("Live poller restarts (24h)", prom('sum(increase(kube_pod_container_status_restarts_total{namespace="$ns", container="live"}[24h]))'),
             decimals=0, thresholds=[(None, "green"), (1, "orange")]),
        stat("Games final today", sql("SELECT count(*) AS value FROM live_games WHERE state = 'post' AND updated_at > date_trunc('day', now())")),
        table("In progress", sql("""
            SELECT lg.league, a.abbreviation || ' ' || lg.away_score || ' – ' || h.abbreviation || ' ' || lg.home_score AS score,
                   lg.detail, lg.down_distance, round(lg.home_win_prob::numeric * 100) AS "home win %",
                   round(extract(epoch FROM now() - lg.updated_at)) AS "updated (s ago)",
                   (SELECT count(*) FROM live_plays p WHERE p.league = lg.league AND p.game_id = lg.game_id) AS plays
            FROM live_games lg JOIN games g USING (league, game_id)
            JOIN teams h ON h.league = g.league AND h.team_id = g.home_team_id
            JOIN teams a ON a.league = g.league AND a.team_id = g.away_team_id
            WHERE lg.state = 'in' ORDER BY lg.league, g.start_time"""), w=24, h=9),
        logs("Live poller", '{namespace="$ns", app="live"}', h=10),
    ], [var_ds(), NS_VAR], time_from="now-6h", refresh="30s",
        desc="Live scores and play-by-play freshness (live.py).")


# --- 4. Newsroom -----------------------------------------------------------------------------------------------------

def newsroom():
    usage = "coalesce((checks->'usage'->>'{}')::numeric, 0)"
    inp, out = usage.format("input_tokens"), usage.format("output_tokens")
    return dashboard("sw-newsroom", "SidelineWire · Newsroom", [
        stat("Published today", sql("SELECT count(*) AS value FROM articles WHERE status = 'published' AND published_at > date_trunc('day', now())")),
        stat("Waiting for review", sql("SELECT count(*) AS value FROM articles WHERE status = 'review'"),
             thresholds=[(None, "green"), (10, "orange"), (25, "red")]),
        stat("Being rewritten", sql("SELECT count(*) AS value FROM articles WHERE status = 'regenerating'"),
             thresholds=[(None, "green"), (1, "blue")]),
        stat("Passed checks (7 days)", sql("""SELECT avg(CASE WHEN jsonb_array_length(coalesce(checks->'problems', '[]')) = 0 THEN 1 ELSE 0 END) AS value
                                               FROM articles WHERE created_at > now() - interval '7 days'"""),
             unit="percentunit", decimals=0, thresholds=[(None, "red"), (0.6, "orange"), (0.85, "green")]),
        stat("OpenAI tokens today", sql(f"SELECT sum({inp} + {out}) AS value FROM articles WHERE updated_at > date_trunc('day', now())"),
             unit="short", decimals=0),
        stat("Est. OpenAI cost (30 days)", sql(f"""SELECT sum({inp}) / 1e6 * $in_price + sum({out}) / 1e6 * $out_price AS value
                                                   FROM articles WHERE updated_at > now() - interval '30 days'"""),
             unit="currencyUSD", decimals=2, desc="Uses the $/1M-token prices in the boxes at the top; set them from your OpenAI pricing page."),
        ts("Articles published per day", sql("""SELECT date_trunc('day', published_at) AS time, kind AS metric, count(*) AS value
                                                 FROM articles WHERE status = 'published' AND $__timeFilter(published_at)
                                                 GROUP BY 1, 2 ORDER BY 1""", fmt="time_series"), bars=True, stack=True),
        ts("OpenAI tokens per day", sql(f"""SELECT date_trunc('day', updated_at) AS time, sum({inp}) AS input, sum({out}) AS output
                                             FROM articles WHERE $__timeFilter(updated_at) GROUP BY 1 ORDER BY 1""", fmt="time_series"),
           bars=True, stack=True),
        table("Recent stories", sql(f"""
            SELECT to_char(coalesce(published_at, updated_at) AT TIME ZONE 'America/New_York', 'Dy HH12:MI AM') AS "when",
                   league, kind, status, headline,
                   jsonb_array_length(coalesce(checks->'problems', '[]')) AS problems,
                   coalesce((checks->>'attempts')::int, 0) AS attempts, ({inp} + {out})::int AS tokens, model
            FROM articles ORDER BY coalesce(published_at, updated_at) DESC LIMIT 40"""), w=24, h=12),
        logs("Newsroom job and worker", '{namespace="$ns", app=~"newsroom|newsroom-worker"}', h=9),
    ], [var_ds(), NS_VAR, var_text("in_price", "Input $/1M tokens", "0"), var_text("out_price", "Output $/1M tokens", "0")],
        time_from="now-30d", refresh="5m", desc="AI newsroom output, review queue, check pass rate and OpenAI usage.")


# --- 5. Models -------------------------------------------------------------------------------------------------------

MODEL_BASE = """
WITH lines AS (
    SELECT game_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY home_spread) AS spread
    FROM odds WHERE league = '$league' AND home_spread IS NOT NULL GROUP BY game_id),
g AS (
    SELECT gm.week, gm.start_time, gm.home_score - gm.away_score AS margin, p.home_win_prob AS prob,
           p.predicted_margin AS pm, l.spread
    FROM games gm
    JOIN predictions p ON p.league = gm.league AND p.game_id = gm.game_id
                      AND p.model = CASE WHEN '$league' = 'nfl' THEN 'linear' ELSE 'xgb' END
    LEFT JOIN lines l ON l.game_id = gm.game_id
    WHERE gm.league = '$league' AND gm.season = $season AND gm.completed AND gm.season_type = 2
      AND gm.home_score IS NOT NULL AND gm.home_score <> gm.away_score)
"""
PICK = "avg(((prob > 0.5) = (margin > 0))::int)"
VEGAS = "avg(CASE WHEN spread IS NOT NULL AND spread <> 0 THEN ((spread < 0) = (margin > 0))::int END)"
ATS = "avg(CASE WHEN spread IS NOT NULL AND margin + spread <> 0 AND pm + spread <> 0 THEN ((pm + spread > 0) = (margin + spread > 0))::int END)"


def models():
    pct = dict(unit="percentunit", decimals=1)
    return dashboard("sw-models", "SidelineWire · Models vs Vegas", [
        stat("Games scored", sql(MODEL_BASE + "SELECT count(*) AS value FROM g")),
        stat("Winners picked (headline model)", sql(MODEL_BASE + f"SELECT {PICK} AS value FROM g"), **pct),
        stat("Vegas favorite won", sql(MODEL_BASE + f"SELECT {VEGAS} AS value FROM g"), **pct),
        stat("Against the spread", sql(MODEL_BASE + f"SELECT {ATS} AS value FROM g"), **pct,
             thresholds=[(None, "red"), (0.5, "orange"), (0.524, "green")], desc="52.4% is break-even at standard -110 odds."),
        stat("Brier score (lower is better)", sql(MODEL_BASE + "SELECT avg(power(prob - (margin > 0)::int, 2)) AS value FROM g"), decimals=3),
        stat("Avg miss on margin (pts)", sql(MODEL_BASE + "SELECT avg(abs(margin - pm)) AS value FROM g"), decimals=1),
        panel("barchart", "Winners picked by week: model vs Vegas", sql(MODEL_BASE + f"""
            SELECT 'Wk ' || week AS week, {PICK} AS model, {VEGAS} AS vegas FROM g GROUP BY week ORDER BY min(start_time)"""),
              w=12, h=9, unit="percentunit", defaults={"min": 0, "max": 1},
              overrides=[{"matcher": {"id": "byName", "options": "week"}, "properties": [{"id": "unit", "value": "string"}]}],
              options={"xField": "week", "legend": {"displayMode": "list", "placement": "bottom"}, "barWidth": 0.7}),
        panel("barchart", "Against the spread by week", sql(MODEL_BASE + f"""
            SELECT 'Wk ' || week AS week, {ATS} AS "against the spread" FROM g GROUP BY week ORDER BY min(start_time)"""),
              w=12, h=9, unit="percentunit", defaults={"min": 0, "max": 1},
              overrides=[{"matcher": {"id": "byName", "options": "week"}, "properties": [{"id": "unit", "value": "string"}]}],
              options={"xField": "week", "legend": {"displayMode": "list", "placement": "bottom"}, "barWidth": 0.7}),
        table("By week", sql(MODEL_BASE + f"""
            SELECT week, count(*) AS games, round(100 * {PICK}, 1) AS "model %", round(100 * {VEGAS}, 1) AS "vegas %",
                   round(100 * {ATS}, 1) AS "ats %", round(avg(abs(margin - pm))::numeric, 1) AS "margin miss"
            FROM g GROUP BY week ORDER BY week"""), w=24, h=9),
    ], [var_ds(), var_custom("league", "League", ["nfl", "cfb"], "nfl"), var_custom("season", "Season", ["2026", "2025", "2024"], "2026")],
        time_from="now-7d", refresh="", desc="Headline independent model (NFL linear, college XGBoost) against the Vegas line, this season.")


# --- 6. Server ---------------------------------------------------------------------------------------------------

def server():
    return dashboard("sw-server", "SidelineWire · Server & GPU", [
        stat("CPU used", prom('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'), unit="percentunit", decimals=0,
             thresholds=[(None, "green"), (0.7, "orange"), (0.9, "red")]),
        stat("Memory used", prom('1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes'), unit="percentunit", decimals=0,
             thresholds=[(None, "green"), (0.8, "orange"), (0.9, "red")]),
        stat("Disk used (/)", prom('1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}'),
             unit="percentunit", decimals=0, thresholds=[(None, "green"), (0.75, "orange"), (0.9, "red")]),
        stat("GPU utilization", prom('max(nvidia_smi_utilization_gpu_ratio)'), unit="percentunit", decimals=0),
        stat("GPU memory used", prom('max(nvidia_smi_memory_used_bytes)'), unit="bytes", decimals=1),
        stat("GPU temperature", prom('max(nvidia_smi_temperature_gpu)'), unit="celsius", decimals=0,
             thresholds=[(None, "green"), (75, "orange"), (85, "red")]),
        ts("CPU by environment", prom('sum by (namespace) (rate(container_cpu_usage_seconds_total{container!="", namespace=~"sidelinewire|football-analyst|ai|monitoring"}[5m]))',
                                      "{{namespace}}"), unit="short", stack=True),
        ts("Memory by environment", prom('sum by (namespace) (container_memory_working_set_bytes{container!="", namespace=~"sidelinewire|football-analyst|ai|monitoring"})',
                                         "{{namespace}}"), unit="bytes", stack=True),
        ts("GPU", [prom('max(nvidia_smi_utilization_gpu_ratio)', "utilization"),
                   prom('max(nvidia_smi_memory_used_bytes) / max(nvidia_smi_memory_total_bytes)', "memory")], unit="percentunit"),
        ts("GPU power and temperature", [prom('max(nvidia_smi_power_draw_watts)', "watts"), prom('max(nvidia_smi_temperature_gpu)', "°C")]),
        ts("Network", [prom('sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|cni.*|flannel.*|docker.*|br-.*"}[5m]))', "in"),
                       prom('sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cni.*|flannel.*|docker.*|br-.*"}[5m]))', "out")], unit="Bps"),
        ts("Disk I/O", [prom('sum(rate(node_disk_read_bytes_total[5m]))', "read"), prom('sum(rate(node_disk_written_bytes_total[5m]))', "write")], unit="Bps"),
        table("Pods restarting (24h)", prom('sort_desc(increase(kube_pod_container_status_restarts_total{namespace=~"sidelinewire|football-analyst|ai|monitoring"}[24h]) > 0)',
                                            instant=True, fmt="table"), w=24, h=7,
              overrides=[{"matcher": {"id": "byRegexp", "options": "^(Time|__name__|endpoint|instance|job|service|uid)$"},
                          "properties": [{"id": "custom.hidden", "value": True}]}]),
    ], [], time_from="now-24h", desc="Host CPU/memory/disk/network, per-environment usage and the GPU (Ollama).")


def main():
    boards = [site_api(), pipeline(), live(), newsroom(), models(), server()]
    docs = []
    for b in boards:
        docs.append({"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": f"dashboard-{b['uid']}", "namespace": "monitoring",
                                  "labels": {"grafana_dashboard": "1"}, "annotations": {"grafana_folder": FOLDER}},
                     "data": {f"{b['uid']}.json": json.dumps(b, indent=1)}})
    out = Path(__file__).with_name("dashboards.yaml")
    out.write_text("# Generated by dashboards.py; edit that file, not this one.\n"
                   + "".join("---\n" + json.dumps(d) + "\n" for d in docs))
    print(f"wrote {out} ({len(boards)} dashboards)")


if __name__ == "__main__":
    main()
