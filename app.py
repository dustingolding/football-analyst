"""Web front end: reads games, lines, ratings and stored predictions from Postgres.

Nothing here fetches data or runs a model; the batch jobs (etl.py, elo.py, features.py,
train.py, ...) write everything this app shows.

    flask --app app run --debug        # local development
    gunicorn app:app                   # production (see Dockerfile)
"""

import functools
import hashlib
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import charts
import web_data
from database import closing_lines, connect

app = Flask(__name__)
SITE_ENV = os.getenv("SITE_ENV", "dev")  # "prod" on sidelinewire.com; anything else is a dev/test site


@app.after_request
def no_index_outside_prod(response):
    """Keep dev.sidelinewire.com (and local runs) out of search results."""
    if SITE_ENV != "prod":
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.url_defaults
def static_cache_buster(endpoint, values):
    """Add ?v=<content hash> to static URLs so browsers and Cloudflare, which cache static files
    for hours, fetch a changed stylesheet right after a deploy instead of serving a stale copy."""
    if endpoint == "static" and "filename" in values:
        values.setdefault("v", _static_version(values["filename"]))


@functools.cache
def _static_version(filename):
    path = os.path.join(app.static_folder, filename)
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:10]
    except OSError:
        return "0"

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
           g.home_rank, g.away_rank, g.venue_name, g.venue_city, g.venue_state, g.broadcast,
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

        # Best available number for each output: market-adjusted, then independent, then Elo
        # (the market model has no win probability when a game has no moneyline).
        order = [g["preds"].get(m) for m in ("xgb_market", independent, "elo") if g["preds"].get(m)]
        pick = lambda key: next((p[key] for p in order if p.get(key) is not None), None)  # noqa: E731
        g["win_prob"] = pick("home_win_prob")
        margin = pick("predicted_margin")
        proj_total = pick("predicted_total") or total
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
    attach_live(league, games)
    return games


def live_rows(league, ids):
    return {r["game_id"]: r for r in query(
        "SELECT game_id, state, detail, period, clock, home_score, away_score, possession_team_id, down_distance, "
        "red_zone, home_timeouts, away_timeouts, home_win_prob, last_play, updated_at FROM live_games "
        "WHERE league = %s AND game_id = ANY(%s)", (league, ids))}


def attach_live(league, games):
    """Overlay live state: g['state'] is pre / in / final, with the scores to display.

    Games finish in live_games minutes before the refresh pipeline marks them completed,
    so a live 'post' counts as final."""
    live = live_rows(league, [g["game_id"] for g in games]) if games else {}
    for g in games:
        row = live.get(g["game_id"])
        g["live"] = row if row and row["state"] in ("in", "post") and not g["completed"] else None
        if g["completed"]:
            g["state"], g["show_home"], g["show_away"] = "final", g["home_score"], g["away_score"]
        elif g["live"]:
            g["state"] = "in" if row["state"] == "in" else "final"
            g["show_home"], g["show_away"] = row["home_score"], row["away_score"]
        else:
            g["state"], g["show_home"], g["show_away"] = "pre", None, None
        g["status_text"] = (row or {}).get("detail") if g["live"] else ("Final" if g["state"] == "final" else None)
        g["kickoff_ts"] = int(g["start_time"].timestamp()) if g.get("start_time") else 0


def live_payload(row):
    return {
        "state": "in" if row["state"] == "in" else ("final" if row["state"] == "post" else "pre"),
        "detail": row["detail"], "home": row["home_score"], "away": row["away_score"],
        "home_win_prob": row["home_win_prob"], "down_distance": row["down_distance"],
        "possession": row["possession_team_id"], "red_zone": row["red_zone"], "last_play": row["last_play"],
    }


@app.route("/api/live/<league>")
def api_live(league):
    """Live state for the requested games (?ids=1,2,3), for pages polling every 30 s."""
    league_or_404(league)
    ids = [i for i in request.args.get("ids", "").split(",") if i.isdigit()][:300]
    rows = live_rows(league, ids) if ids else {}
    response = jsonify({gid: live_payload(r) for gid, r in rows.items() if r["state"] in ("in", "post")})
    response.headers["Cache-Control"] = "no-store"
    return response


def live_panel_context(league, g):
    """Situation, win-probability series and plays for a game's live panel."""
    plays = query(
        "SELECT play_id, sequence, drive, period, clock, team_id, play_type, text, home_score, away_score, scoring, "
        "home_win_prob FROM live_plays WHERE league = %s AND game_id = %s ORDER BY sequence, play_id",
        (league, g["game_id"]))
    wp = charts.win_probability(plays, g["home_abbr"], g["away_abbr"])
    team_abbr = {g["home_team_id"]: g["home_abbr"], g["away_team_id"]: g["away_abbr"]}
    for p in plays:
        p["team_abbr"] = team_abbr.get(p["team_id"], "")
    box = web_data.game_boxscore(league, g["game_id"], g["away_team_id"], g["home_team_id"])
    return {"plays": list(reversed(plays))[:80], "wp": wp, "box": box,
            "live": g.get("live")}


@app.route("/<league>/game/<game_id>/live")
def game_live_panel(league, game_id):
    """The game page's live panel, re-fetched every 30 s by static/live.js."""
    league_or_404(league)
    rows = query(GAME_SQL + " WHERE g.league = %s AND g.game_id = %s", (league, game_id))
    if not rows:
        abort(404)
    g = attach_predictions(league, rows)[0]
    response = app.make_response(render_template("_live_panel.html", g=g, league=league, **live_panel_context(league, g)))
    response.headers["Cache-Control"] = "no-store"
    return response


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


def upcoming_games(league, days=8):
    games = query(GAME_SQL + " WHERE g.league = %s AND NOT g.completed AND g.start_time > now() - interval '6 hours' "
                  "AND g.start_time < now() + %s * interval '1 day' AND g.status NOT IN ('STATUS_CANCELED', "
                  "'STATUS_POSTPONED') ORDER BY g.start_time, g.game_id", (league, days))
    return attach_predictions(league, games)


def last_week_results(league):
    """The most recent fully finished week: its games (with predictions) and our record."""
    row = query("SELECT season, season_type, week FROM games g WHERE league = %s AND completed "
                "AND start_time < now() AND NOT EXISTS (SELECT 1 FROM games o WHERE o.league = g.league "
                "AND o.season = g.season AND o.season_type = g.season_type AND o.week = g.week AND NOT o.completed "
                "AND o.start_time > now() - interval '2 days') "
                "ORDER BY start_time DESC LIMIT 1", (league,))
    if not row:
        return None
    r = row[0]
    games = attach_predictions(league, query(
        GAME_SQL + " WHERE g.league = %s AND g.season = %s AND g.season_type = %s AND g.week = %s AND g.completed",
        (league, r["season"], r["season_type"], r["week"])))
    picks = [g for g in games if g.get("pick_right") is not None]
    leans = [g for g in games if g.get("lean_right") is not None]
    upsets = sorted((g for g in games if g.get("win_prob") is not None and g.get("winner_home") is not None
                     and (g["win_prob"] < 0.5) == g["winner_home"]),
                    key=lambda g: min(g["win_prob"], 1 - g["win_prob"]))
    return {**r, "picks": (sum(g["pick_right"] for g in picks), len(picks)),
            "ats": (sum(g["lean_right"] for g in leans), len(leans)), "upsets": upsets[:3]}


def season_record(league):
    """Our picks this season (completed games): winners and the independent model against the spread."""
    season = web_data.seasons(league)[0]
    games = attach_predictions(league, query(GAME_SQL + " WHERE g.league = %s AND g.season = %s AND g.completed",
                                             (league, season)))
    picks = [g["pick_right"] for g in games if g.get("pick_right") is not None]
    leans = [g["lean_right"] for g in games if g.get("lean_right") is not None]
    return {"season": season, "picks": (sum(picks), len(picks)), "ats": (sum(leans), len(leans))}


def featured_games(league, limit=10):
    """Upcoming/live games worth featuring (college: games with a ranked team)."""
    games = upcoming_games(league)
    if league == "cfb":
        games = [g for g in games if g["home_rank"] or g["away_rank"] or g["state"] == "in"]
    return games[:limit]


def model_edges(league, limit=3):
    """Upcoming games where our line-free model disagrees with Vegas most: real matchups (FBS vs
    FBS in college) with sane spreads; on 30-point lines a big gap is mostly noise."""
    season = web_data.seasons(league)[0]
    candidates = [g for g in upcoming_games(league) if g.get("edge") is not None and abs(g["edge"]) >= 3
                  and abs(g["line"].get("spread") or 99) <= 21
                  and web_data.is_major(league, g["home_team_id"], season)
                  and web_data.is_major(league, g["away_team_id"], season)]
    return sorted(candidates, key=lambda g: -abs(g["edge"]))[:limit]


def power_top(league, n=10):
    season = web_data.seasons(league)[0]
    team_map, recs = web_data.teams(league), web_data.records(league, season)
    rows = sorted((t for t in web_data.power_ratings(league, season).values() if t.get("rank")),
                  key=lambda t: t["rank"])[:n]
    return [dict(t, team=team_map.get(t["team_id"]), record=(recs.get(t["team_id"]) or {}).get("overall", "0-0"))
            for t in rows]


def ap_top(n=10):
    season = web_data.seasons("cfb")[0]
    weeks = web_data.poll_weeks("cfb", season)
    if not weeks:
        return []
    tables = web_data.poll_tables("cfb", season, weeks[0]["season_type"], weeks[0]["week"])
    return next((t["rows"][:n] for t in tables if t["title"] == "AP Top 25"), [])


@app.route("/")
def home():
    lead = web_data.lead_stories(None)
    featured = {league: featured_games(league) for league in LEAGUES}
    edges = [(league, g) for league in LEAGUES for g in model_edges(league)]
    return render_template(
        "home.html", featured=featured, edges=edges, power=power_top("nfl"), ap=ap_top(),
        results={lg: last_week_results(lg) for lg in LEAGUES}, records={lg: season_record(lg) for lg in LEAGUES},
        independent=INDEPENDENT_MODEL, lead=lead, stories=[a for a in web_data.latest_articles(None, 10)
                                                          if a["id"] not in {x["id"] for x in lead}][:6],
        league=None,
    )


@app.route("/<league>/")
def league_home(league):
    """League hub: this week's games, latest stories, ratings, standings snapshot, leaders."""
    league_or_404(league)
    if request.args.get("week") or request.args.get("season"):  # old slate links
        return redirect(url_for("slate", league=league, **request.args))
    season, season_type, week = current_week(league)
    lead = web_data.lead_stories(league)
    games = featured_games(league, limit=12)
    live = [g for g in games if g["state"] == "in"]
    groups = web_data.standings(league, season)
    if league == "nfl":
        leaders_by_group = [(grp["name"], grp["teams"][:1]) for grp in groups]
    else:
        big = ["SEC", "Big Ten", "Big 12", "ACC"]
        leaders_by_group = [(grp["name"], grp["teams"][:2]) for grp in sorted(
            groups, key=lambda grp: (grp["name"] not in big, big.index(grp["name"]) if grp["name"] in big else 0,
                                     grp["name"]))]
    boards = [web_data.player_board(league, season, web_data.find_player_spec(slug), limit=3)
              for slug in ("passing-yards", "rushing-yards", "receiving-yards", "sacks")]
    preseason = web_data.preseason_table(league, season) if season in web_data.preseason_seasons(league) else []
    if league == "cfb":  # chance of a top-10 season
        outlook = sorted((r for r in preseason if r.get("elite_prob") is not None), key=lambda r: -r["elite_prob"])[:6]
    else:  # biggest surprises: playing furthest above their preseason projection
        outlook = sorted((r for r in preseason if r["actual"] is not None), key=lambda r: -(r["actual"] - r["rating"]))[:6]
    return render_template(
        "league_home.html", league=league, league_name=LEAGUES[league], season=season, season_type=season_type,
        week=week, games=games, live=live, edges=model_edges(league), power=power_top(league),
        ap=ap_top() if league == "cfb" else [], leaders_by_group=leaders_by_group, boards=boards, outlook=outlook,
        result=last_week_results(league), record=season_record(league), independent=MODEL_LABELS[INDEPENDENT_MODEL[league]],
        lead=lead, stories=[a for a in web_data.latest_articles(league, 10) if a["id"] not in {x["id"] for x in lead}][:6],
    )


@app.route("/healthz")
def healthz():
    query("SELECT 1 AS ok")
    return {"ok": True}


@app.route("/<league>/games")
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
                           stories=web_data.game_articles(league, game_id),
                           models=models, books=books, context=f, availability=web_data.game_availability(league, g),
                           **live_panel_context(league, g))


@app.route("/<league>/ratings")
def ratings(league):
    league_or_404(league)
    season = web_data.seasons(league)[0]
    recs = web_data.records(league, season)
    team_map = web_data.teams(league)
    teams = sorted((dict(t, team=team_map.get(t["team_id"]), record=(recs.get(t["team_id"]) or {}).get("overall", "0-0"))
                    for t in web_data.power_ratings(league, season).values() if t.get("rank")),
                   key=lambda t: t["rank"])
    return render_template("ratings.html", league=league, league_name=LEAGUES[league], teams=teams, season=season)


@app.route("/<league>/teams")
def teams_page(league):
    league_or_404(league)
    season = web_data.seasons(league)[0]
    groups = web_data.standings(league, season)
    power = web_data.power_ratings(league, season)
    ranks = web_data.latest_ap_ranks(league, season) if league == "cfb" else {}
    for group in groups:
        for t in group["teams"]:
            t["elo_rank"] = (power.get(t["team_id"]) or {}).get("rank")
            t["ap_rank"] = ranks.get(t["team_id"])
    return render_template("teams.html", league=league, league_name=LEAGUES[league], groups=groups, season=season)


def team_schedule(league, season, team_id):
    """A team's season games with predictions, from the team's side: opponent, win probability,
    spread, and for finished games the result, score and whether it covered."""
    games = query(GAME_SQL + " WHERE g.league = %s AND g.season = %s AND (g.home_team_id = %s OR g.away_team_id = %s) "
                  "ORDER BY g.start_time", (league, season, team_id, team_id))
    attach_predictions(league, games)
    for g in games:
        home = g["home_team_id"] == team_id
        g["is_home"] = home
        g["opp"] = {"id": g["away_team_id"] if home else g["home_team_id"],
                    "name": g["away_short"] if home else g["home_short"],
                    "abbr": g["away_abbr"] if home else g["home_abbr"],
                    "logo": g["away_logo"] if home else g["home_logo"],
                    "rank": g["away_rank"] if home else g["home_rank"]}
        g["team_rank"] = g["home_rank"] if home else g["away_rank"]
        g["team_win_prob"] = None if g["win_prob"] is None else (g["win_prob"] if home else 1 - g["win_prob"])
        spread = g["line"].get("spread")
        g["team_spread"] = None if spread is None else (spread if home else -spread)
        if g["completed"] and g["home_score"] is not None:
            us, them = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
            g["result"] = "W" if us > them else "L" if us < them else "T"
            g["score"] = f"{us}-{them}"
            if g["team_spread"] is not None and us - them + g["team_spread"] != 0:
                g["covered"] = us - them + g["team_spread"] > 0
    return games


@app.route("/<league>/teams/<team_id>")
def team_page(league, team_id):
    league_or_404(league)
    team = web_data.teams(league).get(team_id)
    if team is None:
        abort(404)
    season = web_data.resolve_season(league, request.args.get("season", type=int))
    tab = request.args.get("tab", "home")
    if tab not in ("home", "schedule", "stats", "roster", "offseason"):
        tab = "home"

    aff = web_data.affiliations(league, season).get(team_id) or {}
    record = web_data.records(league, season).get(team_id)
    power = web_data.power_ratings(league, season).get(team_id)
    season_stats = web_data.team_seasons(league, season).get(team_id)
    ap_rank = web_data.latest_ap_ranks(league, season).get(team_id) if league == "cfb" else None

    games = team_schedule(league, season, team_id)

    stat_tables = web_data.team_player_stats(league, season, team_id) if tab in ("home", "stats") else {}
    upcoming = next((g for g in games if not g["completed"]), None)
    recent = [g for g in games if g.get("result")][-5:][::-1]
    group = None
    if tab == "home":
        name = aff.get("division") if league == "nfl" else aff.get("conference")
        group = next((grp for grp in web_data.standings(league, season) if grp["name"] == name), None)
    return render_template(
        "team.html", league=league, league_name=LEAGUES[league], team=team, team_id=team_id, season=season,
        seasons=web_data.seasons(league), tab=tab, aff=aff, record=record, power=power, season_stats=season_stats,
        ap_rank=ap_rank, games=games, upcoming=upcoming, recent=recent, group=group, stat_tables=stat_tables,
        leaders=web_data.team_leaders(stat_tables) if stat_tables else [],
        roster=web_data.roster_groups(league, season, team_id) if tab == "roster" else {},
        stat_labels=web_data.STAT_LABELS, decimals=web_data.DECIMALS,
        preseason=web_data.team_preseason(league, season, team_id),
        transfers=(web_data.team_transfers(league, season, team_id) if league == "cfb"
                   else web_data.nfl_moves(season, team_id)[:2]) if tab == "offseason" else ([], []),
        draft=web_data.nfl_moves(season, team_id)[2] if tab == "offseason" and league == "nfl" else [],
        labels=GROUP_LABELS[league],
        elo_chart=charts.elo_history(league, team_id, season) if tab == "home" else None,
        news=web_data.team_news(league, team_id, 6) if tab == "home" else ([], []),
    )


@app.route("/<league>/standings")
def standings_page(league):
    league_or_404(league)
    season = web_data.resolve_season(league, request.args.get("season", type=int))
    groups = web_data.standings(league, season)
    names = [g["name"] for g in groups]
    selected = request.args.get("group") or ("all" if league == "nfl" else (names[0] if names else None))
    if selected != "all" and selected not in names:
        selected = "all"
    shown = groups if selected == "all" else [g for g in groups if g["name"] == selected]
    ranks = web_data.latest_ap_ranks(league, season) if league == "cfb" else {}
    return render_template("standings.html", league=league, league_name=LEAGUES[league], season=season,
                           seasons=web_data.seasons(league), groups=shown, names=names, selected=selected, ranks=ranks)


@app.route("/<league>/rankings")
def rankings_page(league):
    league_or_404(league)
    if league != "cfb":
        return redirect(url_for("ratings", league=league))
    season = web_data.resolve_season(league, request.args.get("season", type=int))
    weeks = web_data.poll_weeks(league, season)
    for w in weeks:
        w["value"] = f"{w['season_type']}:{w['week']}"
        w["label"] = "Final / Postseason" if w["season_type"] == 3 else f"Week {w['week']}"
    selected = next((w for w in weeks if w["value"] == request.args.get("week")), weeks[0] if weeks else None)
    tables = web_data.poll_tables(league, season, selected["season_type"], selected["week"]) if selected else []
    return render_template("rankings.html", league=league, league_name=LEAGUES[league], season=season,
                           seasons=web_data.seasons(league), weeks=weeks, selected=selected, tables=tables)


# Contribution groups share keys across leagues; the NFL labels two of them differently.
GROUP_LABELS = {
    "cfb": {"history": "History", "recruiting": "Recruiting", "returning": "Returning", "transfers": "Transfers",
            "coaching": "Coaching"},
    "nfl": {"history": "History", "recruiting": "Draft", "returning": "Returning", "transfers": "Free agency",
            "coaching": "Coaching"},
}


def preseason_sorts(league):
    labels = GROUP_LABELS[league]
    sorts = {"rating": "Preseason rating", "change": "Change from last season", "transfers": labels["transfers"],
             "returning": "Returning production", "recruiting": labels["recruiting"]}
    if league == "cfb":
        sorts["elite"] = "Elite-season odds"
    return sorts


@app.route("/<league>/preseason")
def preseason_page(league):
    league_or_404(league)
    seasons = web_data.preseason_seasons(league)
    if not seasons:
        abort(404)
    season = request.args.get("season", seasons[0], type=int)
    if season not in seasons:
        season = seasons[0]
    sorts = preseason_sorts(league)
    sort = request.args.get("sort", "rating")
    if sort not in sorts:
        sort = "rating"
    rows = list(web_data.preseason_table(league, season))
    if sort == "change":
        rows.sort(key=lambda r: -(r["change"] or 0))
    elif sort == "elite":
        rows.sort(key=lambda r: -(r["elite_prob"] or 0))
    elif sort != "rating":
        rows.sort(key=lambda r: -r["contributions"].get(sort, 0))
    groups = ["history", "recruiting", "returning", "transfers", "coaching"]
    return render_template("preseason.html", league=league, league_name=LEAGUES[league], season=season,
                           seasons=seasons, rows=rows, sort=sort, sorts=sorts, groups=groups,
                           labels=GROUP_LABELS[league])


def stats_filters(league):
    season = web_data.resolve_season(league, request.args.get("season", type=int))
    groups = sorted({(a["division"] if league == "nfl" else a["conference"])
                     for tid, a in web_data.affiliations(league, season).items()
                     if web_data.is_major(league, tid, season) and (a["division"] if league == "nfl" else a["conference"])})
    if league == "nfl":
        groups = ["AFC", "NFC"] + groups
    group = request.args.get("group") or None
    return season, groups, (group if group in groups else None)


@app.route("/<league>/stats")
def stats_page(league):
    league_or_404(league)
    season, groups, group = stats_filters(league)
    tab = request.args.get("tab", "player")
    if tab not in ("player", "team"):
        tab = "player"
    sections = {}
    if tab == "player":
        for name, specs in web_data.PLAYER_LEADERS.items():
            sections[name] = [web_data.player_board(league, season, spec, group, limit=5) for spec in specs]
        sections["offense"].append(web_data.qb_epa_leaders(league, season, group, limit=5))
    else:
        for name, specs in web_data.TEAM_LEADERS.items():
            sections[name] = [web_data.team_board(league, season, spec, group, limit=5) for spec in specs]
    return render_template("stats.html", league=league, league_name=LEAGUES[league], season=season,
                           seasons=web_data.seasons(league), groups=groups, group=group, tab=tab, sections=sections)


@app.route("/<league>/stats/<tab>/<slug>")
def stats_leaders_page(league, tab, slug):
    league_or_404(league)
    season, groups, group = stats_filters(league)
    if tab == "player" and slug == web_data.QB_EPA_SLUG:
        board = web_data.qb_epa_leaders(league, season, group, limit=100)
    elif tab == "player" and web_data.find_player_spec(slug):
        board = web_data.player_board(league, season, web_data.find_player_spec(slug), group, limit=100)
    elif tab == "team" and web_data.find_team_spec(slug):
        board = web_data.team_board(league, season, web_data.find_team_spec(slug), group, limit=200)
    else:
        abort(404)
    return render_template("stats_leaders.html", league=league, league_name=LEAGUES[league], season=season,
                           seasons=web_data.seasons(league), groups=groups, group=group, tab=tab, board=board)


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

        def calibration(get):  # 10-point bins of predicted home win probability -> actual home win rate
            bins = [[0, 0, 0.0] for _ in range(10)]
            for gid in common:
                prob = get(gid)
                if prob is None or games[gid]["margin"] == 0:
                    continue
                b = bins[min(int(prob * 10), 9)]
                b[0] += 1
                b[1] += games[gid]["margin"] > 0
                b[2] += prob
            return [{"bin": i, "n": n, "actual": w / n, "predicted": p / n} for i, (n, w, p) in enumerate(bins) if n >= 15]

        calib = [(MODEL_LABELS[m], calibration(lambda gid, p=preds[m]: p[gid]["home_win_prob"]))
                 for m in (INDEPENDENT_MODEL[league], "xgb_market") if m in preds]
        calib.append(("Vegas", calibration(lambda gid: lines[gid]["home_prob"])))
        out[league] = {"games": len(common), "rows": rows, "calibration": calib}
    _metrics_cache.update(at=time.time(), data=out)
    return out


@app.route("/models")
def models():
    return render_template("models.html", metrics=compute_metrics(), leagues=LEAGUES,
                           first_season=TEST_FIRST_SEASON)


@app.route("/how-it-works")
def how_it_works():
    """Explainer: Elo, EPA, the models, preseason ratings and how well it all works, with charts."""
    metrics = compute_metrics()
    leagues = {}
    for league in LEAGUES:
        params = query("SELECT details->'params' AS p, (details->>'margin_per_elo')::float AS m FROM predictions "
                       "WHERE league = %s AND model = 'elo' ORDER BY created_at DESC LIMIT 1", (league,))
        season = web_data.seasons(league)[0]
        top = next((t for t in web_data.power_ratings(league, season).values() if t.get("rank") == 1), None)
        preseason = web_data.preseason_table(league, season)
        pre_top = max(preseason, key=lambda r: r["rating"]) if preseason else None
        rows = {label: m for label, _, m in metrics[league]["rows"]}
        leagues[league] = {
            "name": LEAGUES[league], "elo": params[0] if params else None, "season": season,
            "independent": MODEL_LABELS[INDEPENDENT_MODEL[league]],
            "scores": {k: rows.get(v) for k, v in (("elo", "Elo"), ("ind", MODEL_LABELS[INDEPENDENT_MODEL[league]]),
                                                   ("market", "Market-adjusted"), ("vegas", "Vegas closing line"))},
            "games": metrics[league]["games"],
            "top": top and {**top, "team": web_data.teams(league).get(top["team_id"])},
            "elo_chart": charts.elo_history(league, top["team_id"], season) if top else None,
            "pre_top": pre_top,
            "families": charts.model_families(league),
            "calibration": charts.calibration(metrics[league]["calibration"]),
        }
    return render_template("how_it_works.html", data=leagues, first_season=TEST_FIRST_SEASON)


_ticker_cache = {"at": 0.0, "data": None}
TICKER_LIMIT = {"nfl": 12, "cfb": 14}


def ticker():
    """Games for the scoreboard strip above the header, grouped by league: live first, then
    upcoming (next few days), then today's finals. College: all live games (ranked first),
    upcoming and final games with a ranked team."""
    if _ticker_cache["data"] is not None and time.time() - _ticker_cache["at"] < 20:
        return _ticker_cache["data"]
    rows = query(
        """
        SELECT lg.league, lg.game_id, lg.state, lg.detail, lg.home_score, lg.away_score, lg.possession_team_id,
               lg.down_distance, lg.red_zone, lg.broadcast, lg.home_record, lg.away_record, g.start_time,
               g.home_team_id, g.away_team_id, g.home_rank, g.away_rank, g.neutral_site,
               h.abbreviation AS home_abbr, h.logo AS home_logo, a.abbreviation AS away_abbr, a.logo AS away_logo
        FROM live_games lg
        JOIN games g ON g.league = lg.league AND g.game_id = lg.game_id
        JOIN teams h ON h.league = g.league AND h.team_id = g.home_team_id
        JOIN teams a ON a.league = g.league AND a.team_id = g.away_team_id
        WHERE (lg.state = 'in')
           OR (lg.state = 'pre' AND g.start_time BETWEEN now() - interval '1 hour' AND now() + interval '4 days')
           OR (lg.state = 'post' AND g.start_time > now() - interval '20 hours')
        """
    )
    order = {"in": 0, "pre": 1, "post": 2}
    blocks = []
    for league, label in (("nfl", "NFL"), ("cfb", "NCAAF")):
        games = [r for r in rows if r["league"] == league]
        ranked = lambda r: bool(r["home_rank"] or r["away_rank"])  # noqa: E731
        if league == "cfb":  # every live game; upcoming and finals only with a ranked team
            games = [r for r in games if r["state"] == "in" or ranked(r)]
        games.sort(key=lambda r: (order[r["state"]], not ranked(r),
                                  r["start_time"].timestamp() * (-1 if r["state"] == "post" else 1)))
        for r in games:
            r["kickoff_ts"] = int(r["start_time"].timestamp())
            r["ui_state"] = {"in": "in", "pre": "pre", "post": "final"}[r["state"]]
        if games:
            blocks.append({"league": league, "label": label, "games": games[:TICKER_LIMIT[league]]})
    # A league with games in progress leads, so live scores are visible without scrolling.
    blocks.sort(key=lambda b: b["games"][0]["state"] != "in")
    _ticker_cache.update(at=time.time(), data=blocks)
    return blocks


@app.template_filter("ticker_time")
def ticker_time(value):
    local = value.astimezone(EASTERN)
    today = datetime.now(EASTERN).date()
    clock = local.strftime("%-I:%M %p")
    return clock if local.date() == today else f"{local.strftime('%a')} {clock}"


@app.errorhandler(404)
def not_found(err):
    """JSON errors for API paths (apps), the plain page for everything else."""
    if request.path.startswith("/api/"):
        return jsonify({"error": {"code": "not_found", "message": "Not found."}}), 404
    return err


@app.context_processor
def inject_globals():
    return {"leagues": LEAGUES, "now": datetime.now(EASTERN), "ticker": ticker, "site_env": SITE_ENV,
            "contributions_chart": charts.contributions, "calibration_chart": charts.calibration}


# The JSON API for apps (/api/v1); registered last because api.py imports this module.
import metrics  # noqa: E402
from api import bp as api_v1  # noqa: E402
from newsroom_web import bp as newsroom_bp  # noqa: E402
app.register_blueprint(api_v1)
app.register_blueprint(newsroom_bp)
metrics.init(app)
