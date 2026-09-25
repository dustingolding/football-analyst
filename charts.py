"""Chart specs for static/charts.js, each with a data table (the no-JS, screen-reader view).

A spec is plain JSON: {"type": "line" | "bars", ...}. Every builder returns {"spec": ..., "table": {"head", "rows"}}
or None when there is nothing worth drawing. Rendered by the chart() macro in templates/_charts.html.
"""
import math

from web_data import cached, query

# Series slots map to validated palette colors in style.css (.s1 blue, .s2 orange, .s3 aqua).


def pct(v):
    return f"{round(v * 100)}%"


def nice_ticks(lo, hi, count=5):
    """Round tick values covering [lo, hi]."""
    span = (hi - lo) or 1
    raw = span / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t < hi + step * 0.999:
        ticks.append(round(t, 6))
        t += step
    return ticks


def elo_history(league, team_id, season):
    """Team Elo going into each game of `season` and the one before, plus its next game (the current rating)."""
    rows = query(
        """
        SELECT g.game_id, g.season, g.season_type, g.week, g.completed, g.home_team_id, g.home_score, g.away_score,
               p.details->>'home_rating' AS home_rating, p.details->>'away_rating' AS away_rating,
               CASE WHEN g.home_team_id = %(team)s THEN a.abbreviation ELSE h.abbreviation END AS opp
        FROM games g
        JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
        JOIN teams h ON h.league = g.league AND h.team_id = g.home_team_id
        JOIN teams a ON a.league = g.league AND a.team_id = g.away_team_id
        WHERE g.league = %(league)s AND %(team)s IN (g.home_team_id, g.away_team_id)
          AND g.season BETWEEN %(season)s - 1 AND %(season)s
        ORDER BY g.start_time
        """, {"league": league, "team": team_id, "season": season})
    upcoming = [r for r in rows if not r["completed"]]
    rows = [r for r in rows if r["completed"]] + upcoming[:1]
    if len(rows) < 3:
        return None
    points, table, ticks, prev_season = [], [], [], None
    for i, r in enumerate(rows):
        home = r["home_team_id"] == team_id
        rating = float(r["home_rating"] if home else r["away_rating"])
        week = "Bowl/playoff" if r["season_type"] == 3 else f"Wk {r['week']}"
        where = "vs" if home else "@"
        if r["completed"]:
            us, them = (r["home_score"], r["away_score"]) if home else (r["away_score"], r["home_score"])
            result = f"{'W' if us > them else 'L' if us < them else 'T'} {us}-{them}"
            tip = f"{r['season']} {week} · before {where} {r['opp']} ({result})"
        else:
            result = "next"
            tip = f"Now · before {week} {where} {r['opp']}"
        points.append([i, round(rating, 1), tip])
        if r["season"] != prev_season:
            ticks.append([i, str(r["season"])])
            prev_season = r["season"]
        table.append([f"{r['season']} {week}", f"{where} {r['opp']}", result, f"{rating:.0f}"])
    ys = [p[1] for p in points] + [1500]
    y_ticks = nice_ticks(min(ys) - 10, max(ys) + 10, 4)
    spec = {
        "type": "line", "height": 220,
        "x": {"min": 0, "max": len(points) - 1, "ticks": ticks},
        "y": {"min": y_ticks[0], "max": y_ticks[-1], "ticks": y_ticks, "fmt": "int"},
        "refs": [{"y": 1500, "label": "1500 = where every team starts"}],
        "series": [{"name": "Elo", "slot": 1, "points": points}],
    }
    return {"spec": spec, "table": {"head": ["Game", "Opponent", "Result", "Elo before"], "rows": table[::-1]}}


CONTRIB_LABELS = {
    "cfb": [("history", "Program history"), ("recruiting", "Recruiting and talent"), ("returning", "Returning production"),
            ("transfers", "Transfer portal"), ("coaching", "Coaching change")],
    "nfl": [("history", "Team history"), ("recruiting", "Draft"), ("returning", "Returning production"),
            ("transfers", "Free agency and trades"), ("coaching", "Coaching change")],
}


def contributions(league, contribs):
    """Preseason rating broken into points vs an average team."""
    if not contribs:
        return None
    rows = [{"label": label, "value": round(float(contribs.get(key) or 0), 1)} for key, label in CONTRIB_LABELS[league]]
    spec = {"type": "bars", "fmt": "signed1", "unit": "pts", "rows": rows}
    return {"spec": spec, "table": {"head": ["Source", "Points vs average"], "rows": [
        [r["label"], f"{r['value']:+.1f}"] for r in rows]}}


def calibration(bins_by_series):
    """Predicted vs actual home win rate in 10-point bins: [(label, [{bin, n, actual, predicted}])]."""
    series, table = [], {}
    for slot, (label, bins) in enumerate(bins_by_series, start=1):
        pts = []
        for b in bins:
            x = b["bin"] / 10 + 0.05
            pts.append([x, round(b["actual"], 3), f"Predicted {b['bin'] * 10}-{b['bin'] * 10 + 10}%", f"({b['n']} games)"])
            table.setdefault(b["bin"], {})[label] = f"{pct(b['actual'])} of {b['n']}"
        series.append({"name": label, "slot": slot, "points": pts})
    if not any(s["points"] for s in series):
        return None
    ticks = [0, 0.25, 0.5, 0.75, 1]
    spec = {
        "type": "line", "height": 280, "markers": True, "diag": True, "diag_label": "perfect calibration",
        "x": {"min": 0, "max": 1, "ticks": [[t, pct(t)] for t in ticks]},
        "y": {"min": 0, "max": 1, "ticks": ticks, "fmt": "pct"},
        "series": series,
    }
    labels = [s["name"] for s in series]
    rows = [[f"{b * 10}-{b * 10 + 10}%"] + [table[b].get(label, "–") for label in labels] for b in sorted(table)]
    return {"spec": spec, "legend": [(s["slot"], s["name"]) for s in series],
            "table": {"head": ["Predicted home win chance"] + [f"{label}: home teams won" for label in labels], "rows": rows}}


def model_families(league):
    """Share of the XGBoost margin model's gain by feature family (explain.py)."""
    row = cached(("model_explain", league), lambda: query(
        "SELECT data FROM model_explain WHERE league = %s AND model = 'xgb_margin'", (league,)))
    if not row:
        return None
    fams = [f for f in row[0]["data"]["families"] if f["share"] >= 0.005]
    rows = [{"label": f["family"], "value": round(f["share"], 3)} for f in fams]
    spec = {"type": "bars", "fmt": "pct", "unit": "of the model's gain", "rows": rows}
    return {"spec": spec, "table": {"head": ["Feature family", "Share of gain"], "rows": [
        [r["label"], pct(r["value"])] for r in rows]}}


def elapsed_seconds(period, clock):
    """Game seconds elapsed at a play (15-minute quarters; overtime counted as a fifth)."""
    try:
        minutes, seconds = (clock or "0:00").split(":")
        left = int(minutes) * 60 + int(float(seconds))
    except ValueError:
        left = 0
    return (min(period or 1, 5) - 1) * 900 + (900 - min(left, 900))


def win_probability(plays, home_abbr, away_abbr):
    """Home win probability after each play (ESPN) against game time, so a live line fills in left to right.
    `plays` in game order."""
    by_x, table = {}, []
    for p in plays:
        if p.get("home_win_prob") is None:
            continue
        period = p.get("period") or 1
        qlabel = f"Q{period}" if period <= 4 else "OT"
        score = f"{away_abbr} {p.get('away_score') or 0}, {home_abbr} {p.get('home_score') or 0}"
        text = (p.get("text") or p.get("play_type") or "").strip()
        if len(text) > 90:
            text = text[:87] + "…"
        x = elapsed_seconds(period, p.get("clock"))
        by_x[x] = [x, round(p["home_win_prob"], 3), f"{qlabel} {p.get('clock') or ''} · {score}", "· " + text if text else ""]
        if p.get("scoring"):
            table.append([f"{qlabel} {p.get('clock') or ''}", text, score, pct(p["home_win_prob"])])
    pts = sorted(by_x.values())
    if len(pts) < 2:
        return None
    end = max(3600, pts[-1][0])
    ticks = [[q * 900 + 450, f"Q{q + 1}"] for q in range(4)] + ([[3600 + (end - 3600) / 2, "OT"]] if end > 3600 else [])
    spec = {
        "type": "line", "height": 220, "area": True, "area_base": 0.5,
        "x": {"min": 0, "max": end, "ticks": ticks, "tick_marks": [q * 900 for q in range(1, 5) if q * 900 < end]},
        "y": {"min": 0, "max": 1, "ticks": [0, 0.25, 0.5, 0.75, 1], "fmt": "pct"},
        "refs": [{"y": 0.5}],
        "edge": {"top": f"{home_abbr} wins ↑", "bottom": f"{away_abbr} wins ↓"},
        "series": [{"name": f"{home_abbr} win probability", "slot": 1, "points": pts}],
    }
    return {"spec": spec, "last": pts[-1][1],
            "table": {"head": ["When", "Scoring play", "Score", f"{home_abbr} win prob."], "rows": table}}
