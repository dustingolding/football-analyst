"""JSON API for apps (the iOS app): /api/v1/...

Read-only, versioned, and built on the same queries as the website. Conventions:
    - responses are {"data": ..., "meta": {...}}; errors are {"error": {"code": ..., "message": ...}}
    - ids are strings, times are ISO 8601 UTC, keys are snake_case, probabilities are 0-1
    - requests carry an API key in the X-API-Key header (create one with api_keys.py);
      each key has a per-minute rate limit
    - the full schema is at /api/v1/openapi.json (for swift-openapi-generator)

Breaking changes get a new version prefix (/api/v2); fields may be added to v1 at any time.
"""

import hashlib
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, request

import app as site
import web_data
from openapi import SPEC

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
REQUIRE_KEY = os.getenv("API_REQUIRE_KEY", "1") != "0"
_keys_cache = {"at": 0.0, "keys": {}}
_hits = defaultdict(deque)  # key hash -> request times in the last minute (per worker)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@bp.errorhandler(ApiError)
def api_error(err):
    response = jsonify({"error": {"code": err.code, "message": err.message}})
    response.status_code = err.status
    if err.status == 429:
        response.headers["Retry-After"] = "60"
    return response


def active_keys():
    if time.time() - _keys_cache["at"] > 60:
        _keys_cache["keys"] = {r["key_hash"]: r for r in site.query(
            "SELECT key_hash, prefix, name, rate_per_minute FROM api_keys WHERE active")}
        _keys_cache["at"] = time.time()
    return _keys_cache["keys"]


@bp.before_request
def authenticate():
    if request.endpoint == "api_v1.openapi" or not REQUIRE_KEY:
        return
    key = request.headers.get("X-API-Key") or ""
    record = active_keys().get(hashlib.sha256(key.encode()).hexdigest()) if key else None
    if record is None:
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key header.")
    window, now = _hits[record["key_hash"]], time.time()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= record["rate_per_minute"]:
        raise ApiError(429, "rate_limited", f"Limit is {record['rate_per_minute']} requests per minute.")
    window.append(now)


def respond(data, max_age=300, **meta):
    response = jsonify({"data": data, "meta": {"generated_at": iso(datetime.now(timezone.utc)), **meta}})
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    return response


def league_or_error(league):
    if league not in site.LEAGUES:
        raise ApiError(404, "not_found", f"Unknown league {league!r}; use one of {list(site.LEAGUES)}.")
    return league


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def num(value, digits=4):
    return None if value is None else round(float(value), digits)


# --- serializers -------------------------------------------------------------------------------

def team_ref(league, team_id, team=None, rank=None):
    team = team or web_data.teams(league).get(team_id) or {}
    return {"id": team_id, "name": team.get("display_name"), "short_name": team.get("short_name"),
            "abbreviation": team.get("abbreviation"), "logo": team.get("logo"),
            "color": f"#{team['color']}" if team.get("color") else None, "rank": rank}


def game_summary(league, g):
    """A game with its line, our prediction and live state (from app.attach_predictions)."""
    live = g.get("live")
    projected = {"home": num(g.get("proj_home"), 1), "away": num(g.get("proj_away"), 1)} if "proj_home" in g else None
    lean = None
    if g.get("edge") is not None:
        lean = {"team_id": g["home_team_id"] if g["edge"] > 0 else g["away_team_id"], "points": num(abs(g["edge"]), 1),
                "model": site.INDEPENDENT_MODEL[league]}
    return {
        "id": g["game_id"], "league": league, "season": g["season"], "season_type": g["season_type"],
        "week": g["week"], "start_time": iso(g["start_time"]), "neutral_site": bool(g["neutral_site"]),
        "notes": g.get("notes"), "venue": g.get("venue_name"),
        "status": {"state": g["state"], "detail": g.get("status_text")},
        "home": {**team_ref(league, g["home_team_id"], rank=g.get("home_rank")), "score": g.get("show_home")},
        "away": {**team_ref(league, g["away_team_id"], rank=g.get("away_rank")), "score": g.get("show_away")},
        "line": {"home_spread": num(g["line"].get("spread"), 1), "spread_text": g.get("spread_text"),
                 "total": num(g["line"].get("total"), 1), "home_win_prob": num(g["line"].get("home_prob"))},
        "prediction": {"home_win_prob": num(g.get("win_prob")), "projected_score": projected, "lean": lean},
        "live": live_state(live) if live else None,
    }


def live_state(row):
    payload = site.live_payload(row)
    return {"state": payload["state"], "detail": payload["detail"], "home_score": payload["home"],
            "away_score": payload["away"], "possession_team_id": payload["possession"],
            "down_distance": payload["down_distance"], "red_zone": payload["red_zone"],
            "home_win_prob": num(payload["home_win_prob"]), "last_play": payload["last_play"],
            "updated_at": iso(row.get("updated_at"))}


def games_by(league, where, params):
    games = site.query(site.GAME_SQL + " WHERE g.league = %s AND " + where + " ORDER BY g.start_time, g.game_id",
                       (league, *params))
    return site.attach_predictions(league, games)


# --- endpoints ---------------------------------------------------------------------------------

@bp.get("/openapi.json")
def openapi():
    return jsonify(SPEC)


@bp.get("/status")
def status():
    current = {}
    for league in site.LEAGUES:
        season, season_type, week = site.current_week(league)
        current[league] = {"season": season, "season_type": season_type, "week": week}
    return respond({"site_env": site.SITE_ENV, "leagues": list(site.LEAGUES), "current": current}, max_age=60)


@bp.get("/scoreboard")
def scoreboard():
    """The scoreboard strip: live games first, then upcoming, then recent finals, by league."""
    blocks = []
    for block in site.ticker():
        blocks.append({"league": block["league"], "games": [{
            "id": r["game_id"], "start_time": iso(r["start_time"]), "state": r["ui_state"], "detail": r["detail"],
            "broadcast": r["broadcast"], "possession_team_id": r["possession_team_id"],
            "down_distance": r["down_distance"] if r["ui_state"] == "in" else None, "red_zone": r["red_zone"],
            "home": {**team_ref(block["league"], r["home_team_id"], rank=r["home_rank"]), "score": r["home_score"],
                     "record": r["home_record"]},
            "away": {**team_ref(block["league"], r["away_team_id"], rank=r["away_rank"]), "score": r["away_score"],
                     "record": r["away_record"]},
        } for r in block["games"]]})
    return respond(blocks, max_age=15)


@bp.get("/<league>/games")
def games(league):
    league_or_error(league)
    season, season_type, week = site.current_week(league)
    season = request.args.get("season", season, type=int)
    season_type = request.args.get("season_type", season_type, type=int)
    week = request.args.get("week", week, type=int)
    rows = games_by(league, "g.season = %s AND g.season_type = %s AND g.week = %s", (season, season_type, week))
    live = any(g["state"] == "in" for g in rows)
    return respond([game_summary(league, g) for g in rows], max_age=30 if live else 300,
                   season=season, season_type=season_type, week=week)


@bp.get("/<league>/games/<game_id>")
def game(league, game_id):
    league_or_error(league)
    rows = games_by(league, "g.game_id = %s", (game_id,))
    if not rows:
        raise ApiError(404, "not_found", f"No {league} game {game_id}.")
    g = rows[0]
    data = game_summary(league, g)
    data["models"] = [{"model": k, "label": site.MODEL_LABELS.get(k, k), "home_win_prob": num(v["home_win_prob"]),
                       "home_margin": num(v["predicted_margin"], 1), "total": num(v["predicted_total"], 1)}
                      for k, v in g["preds"].items()]
    f = (site.query("SELECT features FROM game_features WHERE league = %s AND game_id = %s", (league, game_id))
         or [{"features": {}}])[0]["features"]
    keys = ["elo", "ewm_margin", "ewm_points_for", "ewm_points_against", "ridge_off_epa", "ridge_def_epa", "qb_rating",
            "rest_days", "preseason_rating"] + (["recruiting_avg", "talent", "returning_ppa_pct"] if league == "cfb" else [])
    data["matchup"] = {side: {k: num(f.get(f"{side}_{k}")) for k in keys} for side in ("home", "away")}
    data["sportsbooks"] = [{"source": b["source"], "provider": b["provider"], "home_spread": num(b["home_spread"], 1),
                            "total": num(b["total"], 1), "home_moneyline": b["home_moneyline"],
                            "away_moneyline": b["away_moneyline"], "opening_home_spread": num(b["opening_home_spread"], 1)}
                           for b in site.query("SELECT source, provider, home_spread, total, home_moneyline, "
                                               "away_moneyline, opening_home_spread FROM odds WHERE league = %s "
                                               "AND game_id = %s ORDER BY source, provider", (league, game_id))]
    availability = web_data.game_availability(league, g)
    data["injuries"] = {side: [{"player": r["player_name"], "position": r["position"], "status": r["status"],
                                "games": r.get("games"), "detail": r.get("detail") or r.get("headline"),
                                "url": r.get("url")} for r in availability[side]] for side in ("home", "away")}
    return respond(data, max_age=30 if g["state"] == "in" else 300)


@bp.get("/<league>/games/<game_id>/live")
def game_live(league, game_id):
    """Poll this during a game: live state, recent plays (newest first) and the win-probability series."""
    league_or_error(league)
    rows = games_by(league, "g.game_id = %s", (game_id,))
    if not rows:
        raise ApiError(404, "not_found", f"No {league} game {game_id}.")
    g = rows[0]
    limit = min(request.args.get("limit", 40, type=int), 200)
    plays = site.query(
        "SELECT play_id, sequence, drive, period, clock, team_id, play_type, text, home_score, away_score, scoring, "
        "home_win_prob FROM live_plays WHERE league = %s AND game_id = %s ORDER BY sequence DESC, play_id DESC",
        (league, game_id))
    return respond({
        "game": game_summary(league, g),
        "plays": [{"id": p["play_id"], "sequence": p["sequence"], "drive": p["drive"], "period": p["period"],
                   "clock": p["clock"], "team_id": p["team_id"], "type": p["play_type"], "text": p["text"],
                   "home_score": p["home_score"], "away_score": p["away_score"], "scoring": bool(p["scoring"]),
                   "home_win_prob": num(p["home_win_prob"])} for p in plays[:limit]],
        "win_probability": [{"sequence": p["sequence"], "period": p["period"], "clock": p["clock"],
                             "home_win_prob": num(p["home_win_prob"])}
                            for p in reversed(plays) if p["home_win_prob"] is not None],
    }, max_age=15)


@bp.get("/<league>/teams")
def teams(league):
    league_or_error(league)
    season = web_data.seasons(league)[0]
    power = web_data.power_ratings(league, season)
    ranks = web_data.latest_ap_ranks(league, season) if league == "cfb" else {}
    out = []
    for group in web_data.standings(league, season):
        for t in group["teams"]:
            out.append({**team_ref(league, t["team_id"], rank=ranks.get(t["team_id"])), "group": group["name"],
                        "record": t["overall"], "elo_rank": (power.get(t["team_id"]) or {}).get("rank")})
    return respond(out, season=season)


def season_arg(league):
    return web_data.resolve_season(league, request.args.get("season", type=int))


def team_or_error(league, team_id):
    if team_id not in web_data.teams(league):
        raise ApiError(404, "not_found", f"No {league} team {team_id}.")


@bp.get("/<league>/teams/<team_id>")
def team(league, team_id):
    league_or_error(league)
    team_or_error(league, team_id)
    season = season_arg(league)
    aff = web_data.affiliations(league, season).get(team_id) or {}
    record = web_data.records(league, season).get(team_id) or {}
    power = web_data.power_ratings(league, season).get(team_id) or {}
    stats = web_data.team_seasons(league, season).get(team_id) or {}
    pre = web_data.team_preseason(league, season, team_id)
    schedule = site.team_schedule(league, season, team_id)
    upcoming = next((g for g in schedule if not g["completed"]), None)
    rank = web_data.latest_ap_ranks(league, season).get(team_id) if league == "cfb" else None
    return respond({
        **team_ref(league, team_id, rank=rank), "season": season, "conference": aff.get("conference"),
        "division": aff.get("division"),
        "record": {"overall": record.get("overall"), "conference": record.get("conf"), "division": record.get("div"),
                   "streak": record.get("streak"), "points_for": record.get("pf"), "points_against": record.get("pa")},
        "ratings": {"elo": num(power.get("elo"), 1), "elo_rank": power.get("rank"), "off_epa": num(power.get("off")),
                    "def_epa": num(power.get("def")), "qb_rating": num(power.get("qb")), "form": num(power.get("form"), 1)},
        "season_stats": {k: num(v, 2) for k, v in stats.items() if k not in ("team_id",)},
        "preseason": None if not pre else {
            "rating": num(pre["rating"], 1), "rank": pre["rank"], "last_season": num(pre["baseline"], 1),
            "actual": num(pre["actual"], 1), "elite_prob": num(pre.get("elite_prob")),
            "contributions": pre["contributions"]},
        "next_game": game_summary(league, upcoming) if upcoming else None,
    })


@bp.get("/<league>/teams/<team_id>/schedule")
def team_schedule(league, team_id):
    league_or_error(league)
    team_or_error(league, team_id)
    season = season_arg(league)
    out = []
    for g in site.team_schedule(league, season, team_id):
        out.append({**game_summary(league, g), "is_home": g["is_home"], "opponent_id": g["opp"]["id"],
                    "result": g.get("result"), "team_spread": num(g.get("team_spread"), 1),
                    "team_win_prob": num(g.get("team_win_prob")), "covered": g.get("covered")})
    return respond(out, season=season)


@bp.get("/<league>/teams/<team_id>/roster")
def team_roster(league, team_id):
    league_or_error(league)
    team_or_error(league, team_id)
    season = season_arg(league)
    groups = web_data.roster_groups(league, season, team_id)
    return respond([{"player_id": p["player_id"], "name": p["name"], "position": p["position"], "jersey": p["jersey"],
                     "height_in": p["height"], "weight_lb": p["weight"], "experience": p["experience"],
                     "origin": p["origin"], "headshot": p["headshot"], "unit": unit}
                    for unit, players in groups.items() for p in players], season=season)


@bp.get("/<league>/teams/<team_id>/stats")
def team_stats(league, team_id):
    league_or_error(league)
    team_or_error(league, team_id)
    season = season_arg(league)
    tables = web_data.team_player_stats(league, season, team_id)
    return respond({title.lower(): [{"name": r["name"], "position": r["position"], "games": r["games"],
                                     **{c: num(r.get(c), 3) for c in cols}} for r in rows]
                    for title, (_, cols, rows) in tables.items()}, season=season)


@bp.get("/<league>/standings")
def standings(league):
    league_or_error(league)
    season = season_arg(league)
    return respond([{"group": grp["name"], "teams": [{
        **team_ref(league, t["team_id"]), "overall": t["overall"], "conference": t["conf"], "division": t["div"],
        "win_pct": num(t["pct"], 3), "points_for": t["pf"], "points_against": t["pa"], "streak": t["streak"]}
        for t in grp["teams"]]} for grp in web_data.standings(league, season)], season=season)


@bp.get("/<league>/rankings")
def rankings(league):
    league_or_error(league)
    if league != "cfb":
        raise ApiError(404, "not_found", "Polls are college football only; see /nfl/ratings.")
    season = season_arg(league)
    weeks = web_data.poll_weeks(league, season)
    if not weeks:
        return respond([], season=season)
    season_type = request.args.get("season_type", weeks[0]["season_type"], type=int)
    week = request.args.get("week", weeks[0]["week"], type=int)
    tables = web_data.poll_tables(league, season, season_type, week)
    return respond([{"poll": t["title"], "entries": [{
        **team_ref(league, r["team_id"]), "rank": r["rank"], "points": r["points"],
        "first_place_votes": r["first_place_votes"], "record": r["record"], "movement": r["movement"], "new": r["new"]}
        for r in t["rows"]]} for t in tables], season=season, season_type=season_type, week=week)


@bp.get("/<league>/ratings")
def ratings(league):
    league_or_error(league)
    season = web_data.seasons(league)[0]
    rows = sorted((t for t in web_data.power_ratings(league, season).values() if t.get("rank")), key=lambda t: t["rank"])
    return respond([{**team_ref(league, t["team_id"]), "rank": t["rank"], "elo": num(t["elo"], 1),
                     "off_epa": num(t["off"]), "def_epa": num(t["def"]), "qb_rating": num(t["qb"]),
                     "form": num(t["form"], 1)} for t in rows], season=season)


@bp.get("/<league>/preseason")
def preseason(league):
    league_or_error(league)
    seasons = web_data.preseason_seasons(league)
    season = request.args.get("season", seasons[0] if seasons else None, type=int)
    rows = web_data.preseason_table(league, season) if season in seasons else []
    return respond([{**team_ref(league, r["team_id"]), "rank": r["rank"], "rating": num(r["rating"], 1),
                     "last_season": num(r["baseline"], 1), "actual": num(r["actual"], 1),
                     "elite_prob": num(r.get("elite_prob")), "contributions": r["contributions"],
                     "transfers_in": r["n_in"], "transfers_out": r["n_out"]} for r in rows], season=season)


@bp.get("/<league>/leaders")
def leaders(league):
    """Top 10 in every player or team category; ?type=player|team&season=&group=."""
    league_or_error(league)
    season = season_arg(league)
    kind = request.args.get("type", "player")
    group = request.args.get("group") or None
    if kind == "player":
        boards = [web_data.player_board(league, season, spec, group) for specs in web_data.PLAYER_LEADERS.values()
                  for spec in specs] + [web_data.qb_epa_leaders(league, season, group)]
    elif kind == "team":
        boards = [web_data.team_board(league, season, spec, group) for specs in web_data.TEAM_LEADERS.values()
                  for spec in specs]
    else:
        raise ApiError(400, "bad_request", "type must be 'player' or 'team'.")
    return respond([{"category": b["slug"], "title": b["title"], "entries": [{
        "name": r.get("name"), "position": r.get("position"), "team_id": r["team_id"], "games": r["games"],
        "value": r["value"]} for r in b["rows"]]} for b in boards], season=season, type=kind, group=group)


@bp.get("/models")
def models():
    return respond({league: {"games": m["games"], "test_first_season": site.TEST_FIRST_SEASON, "models": [
        {"model": label, "description": note, **{k: num(v) for k, v in metrics.items()}}
        for label, note, metrics in m["rows"]]} for league, m in site.compute_metrics().items()}, max_age=3600)


@bp.route("/<path:unused>")
def not_found(unused):
    raise ApiError(404, "not_found", "No such endpoint; see /api/v1/openapi.json.")
