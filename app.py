"""Web front end: reads games, lines, ratings and stored predictions from Postgres.

Nothing here fetches data or runs a model; the batch jobs (etl.py, elo.py, features.py,
train.py, ...) write everything this app shows.

    flask --app app run --debug        # local development
    gunicorn app:app                   # production (see Dockerfile)
"""

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template, request, url_for

from database import closing_lines, connect

app = Flask(__name__)

EASTERN = ZoneInfo("America/New_York")
LEAGUES = {"nfl": "NFL", "cfb": "College Football"}
SEASON_TYPES = {1: "Preseason", 2: "Regular Season", 3: "Postseason"}
# Model shown as "our" independent number per league: the one that did best on the 2022
# validation season without seeing betting lines (see train.py).
INDEPENDENT_MODEL = {"nfl": "linear", "cfb": "xgb"}
MODEL_LABELS = {
    "elo": "Elo",
    "xgb": "XGBoost",
    "linear": "Linear",
    "xgb_market": "Market-adjusted",
}
MODEL_NOTES = {
    "elo": "Team ratings updated after every game; the baseline.",
    "xgb": "Gradient-boosted trees on pre-game features, no betting lines.",
    "linear": "Regression on a dozen strong features, no betting lines.",
    "xgb_market": "Starts from the closing line and learns small corrections.",
}
TEST_FIRST_SEASON = 2023  # train.py: train 2006-2021, validate 2022, test after


def league_or_404(league):
    if league not in LEAGUES:
        abort(404)
    return league


def query(sql, params=()):
    with connect() as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.template_filter("kickoff")
def kickoff(value):
    if value is None:
        return "TBD"
    local = value.astimezone(EASTERN)
    return local.strftime("%a %b %-d, %-I:%M %p")


@app.template_filter("pct")
def pct(value):
    return "–" if value is None else f"{value * 100:.0f}%"


@app.template_filter("signed")
def signed(value, digits=1):
    if value is None:
        return "–"
    return f"{value:+.{digits}f}"


@app.template_filter("num")
def num(value, digits=1):
    if value is None:
        return "–"
    return f"{value:.{digits}f}"


def spread_text(game, spread):
    """-3.5 from the home side -> 'KC -3.5'."""
    if spread is None:
        return "–"
    if spread == 0:
        return "PK"
    if spread < 0:
        return f"{game['home_abbr']} {spread:g}"
    return f"{game['away_abbr']} {-spread:g}"


GAME_SQL = """
    SELECT g.game_id, g.season, g.season_type, g.week, g.start_time, g.status, g.completed,
           g.neutral_site, g.notes, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
           g.home_rank, g.away_rank, g.venue_name, g.venue_city, g.venue_state,
           h.abbreviation AS home_abbr, h.display_name AS home_name, h.short_name AS home_short,
           h.logo AS home_logo, h.color AS home_color,
           a.abbreviation AS away_abbr, a.display_name AS away_name, a.short_name AS away_short,
           a.logo AS away_logo, a.color AS away_color
    FROM games g
    JOIN teams h ON h.league = g.league AND h.team_id = g.home_team_id
    JOIN teams a ON a.league = g.league AND a.team_id = g.away_team_id
"""


def attach_predictions(league, games):
    """Add lines, model predictions and derived picks to each game dict."""
    if not games:
        return games
    ids = [g["game_id"] for g in games]
    preds = {}
    for row in query(
        "SELECT game_id, model, home_win_prob, predicted_margin, predicted_total, details "
        "FROM predictions WHERE league = %s AND game_id = ANY(%s)",
        (league, ids),
    ):
        preds.setdefault(row["game_id"], {})[row["model"]] = row
    with connect() as conn:
        lines = closing_lines(conn, league, ids)

    independent = INDEPENDENT_MODEL[league]
    for g in games:
        g["preds"] = preds.get(g["game_id"], {})
        g["line"] = lines.get(g["game_id"], {})
        spread, total = g["line"].get("spread"), g["line"].get("total")
        g["spread_text"] = spread_text(g, spread)
        g["total"] = total

        best = g["preds"].get("xgb_market") or g["preds"].get(independent) or g["preds"].get("elo")
        g["win_prob"] = best["home_win_prob"] if best else None
        margin = best["predicted_margin"] if best else None
        proj_total = (best or {}).get("predicted_total") or total
        if margin is not None and proj_total is not None:
            g["proj_home"] = (proj_total + margin) / 2
            g["proj_away"] = (proj_total - margin) / 2

        # Our independent number against the spread: which side it prefers and by how much.
        ind = g["preds"].get(independent)
        g["edge"] = None
        if ind and ind["predicted_margin"] is not None and spread is not None:
            edge = ind["predicted_margin"] + spread  # >0: model thinks home covers
            g["edge"] = edge
            g["lean"] = g["home_abbr"] if edge > 0 else g["away_abbr"]

        if g["completed"] and g["home_score"] is not None:
            actual = g["home_score"] - g["away_score"]
            g["winner_home"] = actual > 0
            if g["win_prob"] is not None and actual != 0:
                g["pick_right"] = (g["win_prob"] > 0.5) == (actual > 0)
            if g["edge"] is not None and actual + spread != 0 and abs(g["edge"]) >= 0.5:
                g["lean_right"] = (g["edge"] > 0) == (actual + spread > 0)
    return games


def current_week(league):
    """(season, season_type, week) of the earliest unfinished week, else the latest week."""
    row = query(
        "SELECT season, season_type, week FROM games WHERE league = %s AND NOT completed "
        "AND start_time > now() - interval '4 days' ORDER BY start_time LIMIT 1",
        (league,),
    )
    if not row:
        row = query(
            "SELECT season, season_type, week FROM games WHERE league = %s ORDER BY start_time DESC LIMIT 1",
            (league,),
        )
    r = row[0]
    return r["season"], r["season_type"], r["week"]


@app.route("/")
def home():
    return redirect(url_for("slate", league="nfl"))


@app.route("/healthz")
def healthz():
    query("SELECT 1 AS ok")
    return {"ok": True}


@app.route("/<league>/")
def slate(league):
    league_or_404(league)
    season, season_type, week = current_week(league)
    season = request.args.get("season", season, type=int)
    season_type = request.args.get("type", season_type, type=int)
    week = request.args.get("week", week, type=int)

    games = query(GAME_SQL + " WHERE g.league = %s AND g.season = %s AND g.season_type = %s AND g.week = %s "
                  "ORDER BY g.start_time, g.game_id", (league, season, season_type, week))
    attach_predictions(league, games)

    weeks = query("SELECT DISTINCT season_type, week FROM games WHERE league = %s AND season = %s "
                  "ORDER BY season_type, week", (league, season))
    seasons = [r["season"] for r in query(
        "SELECT DISTINCT season FROM games WHERE league = %s ORDER BY season DESC", (league,))]

    finals = [g for g in games if g.get("pick_right") is not None]
    leans = [g for g in games if g.get("lean_right") is not None]
    summary = {
        "picks": (sum(g["pick_right"] for g in finals), len(finals)),
        "ats": (sum(g["lean_right"] for g in leans), len(leans)),
    }
    return render_template(
        "slate.html", league=league, league_name=LEAGUES[league], games=games, season=season,
        season_type=season_type, week=week, weeks=weeks, seasons=seasons, season_types=SEASON_TYPES,
        summary=summary, independent=MODEL_LABELS[INDEPENDENT_MODEL[league]],
    )


@app.route("/<league>/game/<game_id>")
def game(league, game_id):
    league_or_404(league)
    rows = query(GAME_SQL + " WHERE g.league = %s AND g.game_id = %s", (league, game_id))
    if not rows:
        abort(404)
    g = attach_predictions(league, rows)[0]
    features = query("SELECT features FROM game_features WHERE league = %s AND game_id = %s", (league, game_id))
    f = features[0]["features"] if features else {}
    books = query(
        "SELECT source, provider, home_spread, total, home_moneyline, away_moneyline, opening_home_spread "
        "FROM odds WHERE league = %s AND game_id = %s ORDER BY source, provider",
        (league, game_id),
    )
    for b in books:
        b["spread_text"] = spread_text(g, float(b["home_spread"]) if b["home_spread"] is not None else None)

    # (label, home value, away value, digits, higher-is-better)
    matchup = [
        ("Elo rating", f.get("home_elo"), f.get("away_elo"), 0, True),
        ("Recent margin (weighted)", f.get("home_ewm_margin"), f.get("away_ewm_margin"), 1, True),
        ("Points scored (weighted)", f.get("home_ewm_points_for"), f.get("away_ewm_points_for"), 1, True),
        ("Points allowed (weighted)", f.get("home_ewm_points_against"), f.get("away_ewm_points_against"), 1, False),
        ("Offense EPA/play (adj.)", f.get("home_ridge_off_epa"), f.get("away_ridge_off_epa"), 3, True),
        ("Defense EPA/play allowed (adj.)", f.get("home_ridge_def_epa"), f.get("away_ridge_def_epa"), 3, False),
        ("Starting QB EPA/dropback", f.get("home_qb_rating"), f.get("away_qb_rating"), 3, True),
        ("Rest days", f.get("home_rest_days"), f.get("away_rest_days"), 0, True),
    ]
    if league == "cfb":
        matchup += [
            ("Recruiting (4-class avg)", f.get("home_recruiting_avg"), f.get("away_recruiting_avg"), 1, True),
            ("Talent composite", f.get("home_talent"), f.get("away_talent"), 1, True),
            ("Returning production", f.get("home_returning_ppa_pct"), f.get("away_returning_ppa_pct"), 2, True),
            ("Last season SP+", f.get("home_prev_sp_rating"), f.get("away_prev_sp_rating"), 1, True),
        ]
    matchup = [m for m in matchup if m[1] is not None or m[2] is not None]
    models = [(MODEL_LABELS.get(k, k), MODEL_NOTES.get(k, ""), v) for k, v in sorted(
        g["preds"].items(), key=lambda kv: list(MODEL_LABELS).index(kv[0]) if kv[0] in MODEL_LABELS else 99)]
    return render_template("game.html", league=league, league_name=LEAGUES[league], g=g, matchup=matchup,
                           models=models, books=books, context=f)


@app.route("/<league>/ratings")
def ratings(league):
    league_or_404(league)
    season = query("SELECT max(season) AS s FROM games WHERE league = %s", (league,))[0]["s"]
    # Each team's ratings going into its next game (or its last one if the season is over).
    rows = query(
        """
        WITH team_games AS (
            SELECT gf.game_id, gf.start_time, gf.completed, gf.features, g.home_team_id AS team, 'home' AS side
            FROM game_features gf JOIN games g USING (league, game_id)
            WHERE gf.league = %(league)s AND gf.season = %(season)s
            UNION ALL
            SELECT gf.game_id, gf.start_time, gf.completed, gf.features, g.away_team_id, 'away'
            FROM game_features gf JOIN games g USING (league, game_id)
            WHERE gf.league = %(league)s AND gf.season = %(season)s
        ), pick AS (
            SELECT DISTINCT ON (team) team, side, features
            FROM team_games
            ORDER BY team, completed, CASE WHEN completed THEN -extract(epoch FROM start_time)
                                           ELSE extract(epoch FROM start_time) END
        )
        SELECT t.team_id, t.display_name, t.abbreviation, t.logo, p.side, p.features
        FROM pick p JOIN teams t ON t.league = %(league)s AND t.team_id = p.team
        """,
        {"league": league, "season": season},
    )
    records = {r["team"]: r for r in query(
        """
        SELECT team, sum(win) AS wins, sum(loss) AS losses FROM (
            SELECT home_team_id AS team, (home_score > away_score)::int AS win, (home_score < away_score)::int AS loss
            FROM games WHERE league = %(league)s AND season = %(season)s AND completed AND season_type = 2
            UNION ALL
            SELECT away_team_id, (away_score > home_score)::int, (away_score < home_score)::int
            FROM games WHERE league = %(league)s AND season = %(season)s AND completed AND season_type = 2
        ) x GROUP BY team
        """,
        {"league": league, "season": season},
    )}
    teams = []
    for r in rows:
        f, side = r["features"], r["side"]
        rec = records.get(r["team_id"], {})
        teams.append({
            "name": r["display_name"], "abbr": r["abbreviation"], "logo": r["logo"],
            "record": f"{rec.get('wins', 0)}-{rec.get('losses', 0)}",
            "elo": f.get(f"{side}_elo"), "off": f.get(f"{side}_ridge_off_epa"), "def": f.get(f"{side}_ridge_def_epa"),
            "qb": f.get(f"{side}_qb_rating"), "form": f.get(f"{side}_ewm_margin"),
        })
    teams = [t for t in teams if t["elo"] is not None]
    teams.sort(key=lambda t: -t["elo"])
    if league == "cfb":
        teams = [t for t in teams if t["record"] != "0-0" or t["elo"] > 1400][:150]
    return render_template("ratings.html", league=league, league_name=LEAGUES[league], teams=teams, season=season)


_metrics_cache = {"at": 0.0, "data": None}


def compute_metrics():
    """Test-season scorecard per league: every model and Vegas on the same games."""
    if _metrics_cache["data"] is not None and time.time() - _metrics_cache["at"] < 600:
        return _metrics_cache["data"]
    out = {}
    for league in LEAGUES:
        games = {g["game_id"]: g for g in query(
            "SELECT game_id, home_score - away_score AS margin, home_score + away_score AS total FROM games "
            "WHERE league = %s AND completed AND season >= %s AND home_score IS NOT NULL",
            (league, TEST_FIRST_SEASON))}
        with connect() as conn:
            lines = {k: v for k, v in closing_lines(conn, league, list(games)).items()
                     if v["spread"] is not None and v["home_prob"] is not None and v["total"] is not None}
        preds = {}
        for row in query("SELECT game_id, model, home_win_prob, predicted_margin, predicted_total FROM predictions "
                         "WHERE league = %s AND game_id = ANY(%s)", (league, list(games))):
            preds.setdefault(row["model"], {})[row["game_id"]] = row
        common = [gid for gid in lines if all(gid in p for p in preds.values())]

        def score(get):
            ll = br = acc = mae = tmae = 0.0
            n = nt = ats_n = ats = 0
            for gid in common:
                g = games[gid]
                prob, margin, total = get(gid)
                if g["margin"] != 0 and prob is not None:
                    y = 1.0 if g["margin"] > 0 else 0.0
                    p = min(max(prob, 1e-6), 1 - 1e-6)
                    ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
                    br += (p - y) ** 2
                    acc += (p > 0.5) == (y == 1)
                    n += 1
                if margin is not None:
                    mae += abs(g["margin"] - margin)
                    cover = g["margin"] + lines[gid]["spread"]
                    edge = margin + lines[gid]["spread"]
                    if cover != 0 and abs(edge) > 1e-9:
                        ats += (edge > 0) == (cover > 0)
                        ats_n += 1
                if total is not None:
                    tmae += abs(g["total"] - total)
                    nt += 1
            return {
                "log_loss": ll / n if n else None, "brier": br / n if n else None, "accuracy": acc / n if n else None,
                "margin_mae": mae / len(common) if common else None, "total_mae": tmae / nt if nt else None,
                "ats": ats / ats_n if ats_n else None,
            }

        rows = []
        for model in MODEL_LABELS:
            if model in preds:
                p = preds[model]
                rows.append((MODEL_LABELS[model], MODEL_NOTES[model], score(
                    lambda gid, p=p: (p[gid]["home_win_prob"], p[gid]["predicted_margin"], p[gid]["predicted_total"]))))
        vegas = score(lambda gid: (lines[gid]["home_prob"], -lines[gid]["spread"], lines[gid]["total"]))
        vegas["ats"] = None
        rows.append(("Vegas closing line", "Median across sportsbooks; the benchmark.", vegas))
        out[league] = {"games": len(common), "rows": rows}
    _metrics_cache.update(at=time.time(), data=out)
    return out


@app.route("/models")
def models():
    return render_template("models.html", metrics=compute_metrics(), leagues=LEAGUES,
                           first_season=TEST_FIRST_SEASON)


@app.context_processor
def inject_globals():
    return {"leagues": LEAGUES, "now": datetime.now(EASTERN)}
