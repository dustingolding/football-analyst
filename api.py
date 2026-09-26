"""JSON API for apps (the iOS app): /api/v1/...

Read-only apart from device registration for push notifications (PUT/DELETE /devices/<id>), mock betting,
chat and accounts (Sign in with Apple; signed-in calls add "Authorization: Bearer <session token>"), versioned, and built on the same queries as the website. Conventions:
    - responses are {"data": ..., "meta": {...}}; errors are {"error": {"code": ..., "message": ...}}
    - ids are strings, times are ISO 8601 UTC, keys are snake_case, probabilities are 0-1
    - requests carry either an install token in X-Install-Token (apps: POST /installs trades the app's
      built-in bootstrap key for one) or an API key in X-API-Key (create one with api_keys.py);
      each token and key has a per-minute rate limit
    - the full schema is at /api/v1/openapi.json (for swift-openapi-generator)

Breaking changes get a new version prefix (/api/v2); fields may be added to v1 at any time.
"""

import hashlib
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import psycopg
from flask import Blueprint, abort, g, jsonify, request

import accounts
import app as site
import betting
import chat
import web_data
from database import connect
from openapi import SPEC

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
SITE_URL = "https://sidelinewire.com" if site.SITE_ENV == "prod" else "https://dev.sidelinewire.com"  # invite links
REQUIRE_KEY = os.getenv("API_REQUIRE_KEY", "1") != "0"
_keys_cache = {"at": 0.0, "keys": {}}
_hits = defaultdict(deque)  # key or token hash -> request times in the last minute (per worker)
_tokens_cache = {}          # token hash -> (fetched at, record or None), per worker
TOKEN_CACHE_SECONDS = 60    # how long a revocation can take to reach a worker
_mints = defaultdict(deque)  # client address -> install tokens minted in the last hour (per worker)
MINTS_PER_HOUR = 20
BOOTSTRAP_ENDPOINTS = {"api_v1.create_install_token"}


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
            "SELECT key_hash, prefix, name, rate_per_minute, scope FROM api_keys WHERE active")}
        _keys_cache["at"] = time.time()
    return _keys_cache["keys"]


def install_token(token):
    """The install token's record, or None if it's unknown or revoked. Cached per worker for a minute, which is
    also how often a token's last_seen is refreshed."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    hit = _tokens_cache.get(token_hash)
    if hit and time.time() - hit[0] < TOKEN_CACHE_SECONDS:
        return hit[1]
    if len(_tokens_cache) > 50_000:
        _tokens_cache.clear()
    with connect() as conn:
        row = conn.execute(
            "UPDATE install_tokens SET last_seen_at = now() WHERE token_hash = %s AND revoked_at IS NULL "
            "RETURNING install_id, rate_per_minute", (token_hash,)).fetchone()
    record = {"key_hash": token_hash, "install_id": row[0], "rate_per_minute": row[1]} if row else None
    _tokens_cache[token_hash] = (time.time(), record)
    return record


def rate_limit(key_hash, per_minute):
    window, now = _hits[key_hash], time.time()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= per_minute:
        raise ApiError(429, "rate_limited", f"Limit is {per_minute} requests per minute.")
    window.append(now)


@bp.before_request
def authenticate():
    if request.endpoint == "api_v1.openapi" or not REQUIRE_KEY:
        return
    token = request.headers.get("X-Install-Token") or ""
    if token:
        record = install_token(token)
        if record is None:
            raise ApiError(401, "invalid_token", "This install's token is invalid or revoked; get a new one from "
                                                 "POST /installs.")
        rate_limit(record["key_hash"], record["rate_per_minute"])
        g.api_key_prefix, g.api_key_name, g.install_id = "install", "install token", record["install_id"]
        return
    key = request.headers.get("X-API-Key") or ""
    record = active_keys().get(hashlib.sha256(key.encode()).hexdigest()) if key else None
    if record is None:
        raise ApiError(401, "unauthorized", "Missing or invalid X-API-Key header.")
    if record["scope"] == "bootstrap" and request.endpoint not in BOOTSTRAP_ENDPOINTS:
        raise ApiError(403, "bootstrap_only", "This key can only create install tokens (POST /installs).")
    rate_limit(record["key_hash"], record["rate_per_minute"])
    g.api_key_prefix = record["prefix"]
    g.api_key_name = record["name"]


def client_address():
    return (request.headers.get("CF-Connecting-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[0]
            or request.remote_addr or "?").strip()


@bp.post("/installs")
def create_install_token():
    """Trade an API key (the app's built-in bootstrap key) for this install's own token, sent from then on as
    X-Install-Token. An install keeps its five newest tokens (the app and its widgets can each ask)."""
    body = request.get_json(silent=True) or {}
    install_id = install_id_or_error(str(body.get("install_id") or ""))
    window, now = _mints[client_address()], time.time()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= MINTS_PER_HOUR:
        raise ApiError(429, "rate_limited", "Too many new installs from this network; try again later.")
    window.append(now)
    token = "sli_" + secrets.token_urlsafe(32)
    version = str(body.get("app_version") or "")[:32] or None
    with connect() as conn, conn.transaction():
        conn.execute("INSERT INTO install_tokens (token_hash, install_id, api_key_prefix, app_version) "
                     "VALUES (%s, %s, %s, %s)",
                     (hashlib.sha256(token.encode()).hexdigest(), install_id, g.get("api_key_prefix"), version))
        conn.execute(
            "UPDATE install_tokens SET revoked_at = now() WHERE install_id = %(i)s AND revoked_at IS NULL "
            "AND token_hash NOT IN (SELECT token_hash FROM install_tokens WHERE install_id = %(i)s "
            "AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 5)", {"i": install_id})
    return no_store(respond({"token": token, "install_id": install_id}))


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
        # The schedule's networks; the live feed's (current week only) fills in anything not in it yet.
        "broadcast": g.get("broadcast") or (live or {}).get("broadcast"),
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


def live_max_age(games):
    """How long clients may cache a list of games: briefly while one is live or about to kick off, so clocks and
    scores keep up (the scoreboard's 15 s), otherwise five minutes."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    busy = any(g["state"] == "in" or (g["state"] == "pre" and g.get("start_time") and g["start_time"] <= soon)
               for g in games)
    return 15 if busy else 300


@bp.get("/<league>/games")
def games(league):
    league_or_error(league)
    season, season_type, week = site.current_week(league)
    season = request.args.get("season", season, type=int)
    season_type = request.args.get("season_type", season_type, type=int)
    week = request.args.get("week", week, type=int)
    rows = games_by(league, "g.season = %s AND g.season_type = %s AND g.week = %s", (season, season_type, week))
    return respond([game_summary(league, g) for g in rows], max_age=live_max_age(rows),
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
    data["box_score"] = box_score(league, g)
    stats = web_data.game_team_stats(league, game_id, g["home_team_id"], g["away_team_id"])
    data["team_stats"] = stats and {side: {k: num(v, 4) for k, v in stats[side].items()} for side in ("home", "away")}
    articles = web_data.game_articles(league, game_id)  # {kind: article}
    stories = [articles[k] for k in ("preview", "recap") if k in articles]
    tags = web_data.article_team_tags([a["id"] for a in stories])
    data["stories"] = [article_item(a, tags=tags.get(a["id"], [])) for a in stories]
    return respond(data, max_age=30 if g["state"] == "in" else 300)


def box_score(league, g):
    """Player box score by category, in ESPN's columns: {final, categories: [{name, title, labels, home, away}]}."""
    box = web_data.game_boxscore(league, g["game_id"], g["away_team_id"], g["home_team_id"])
    if not box:
        return None
    side = lambda c: c and {"players": [{"id": p["id"], "name": p["name"], "stats": p["stats"]} for p in c["players"]],  # noqa: E731
                            "totals": c["totals"] or None}
    return {"final": box["final"], "categories": [
        {"name": c["name"], "title": c["title"], "labels": (c["away"] or c["home"])["labels"],
         "home": side(c["home"]), "away": side(c["away"])} for c in box["categories"]]}


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
    out, rows = [], site.team_schedule(league, season, team_id)
    for g in rows:
        out.append({**game_summary(league, g), "is_home": g["is_home"], "opponent_id": g["opp"]["id"],
                    "result": g.get("result"), "team_spread": num(g.get("team_spread"), 1),
                    "team_win_prob": num(g.get("team_win_prob")), "covered": g.get("covered")})
    return respond(out, max_age=live_max_age(rows), season=season)


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


def team_tag(t):
    return {"id": t["team_id"], "name": t["name"], "short_name": t["short"], "abbreviation": t["abbr"], "logo": t["logo"],
            "role": t["role"]}


def article_item(a, body=False, tags=None):
    item = {"id": str(a["id"]), "slug": a["slug"], "league": a["league"], "kind": a["kind"], "game_id": a["game_id"],
            "headline": a["headline"], "dek": a["dek"], "published_at": iso(a["published_at"]),
            "url": f"https://{request.host}/{a['league']}/news/{a['slug']}",
            "teams": [team_tag(t) for t in (tags if tags is not None else web_data.article_team_tags([a["id"]]).get(a["id"], []))]}
    if body:
        item["paragraphs"] = [p.strip() for p in (a.get("body") or "").split("\n\n") if p.strip()]
    return item


@bp.get("/<league>/articles")
def articles(league):
    league_or_error(league)
    kind = request.args.get("kind")
    if kind and kind not in web_data.KIND_LABELS:
        raise ApiError(400, "bad_request", f"kind must be one of {list(web_data.KIND_LABELS)}.")
    limit = min(max(request.args.get("limit", 20, type=int), 1), 50)
    team_ids = team_ids_arg()
    if team_ids:
        ids = {i["article_id"] for i in web_data.news_feed(league, team_ids, None, 200) if i["source"] == "sidelinewire"}
        rows = [a for a in web_data.latest_articles(league, 200, kind) if a["id"] in ids][:limit]
    else:
        rows = web_data.latest_articles(league, limit, kind, max(request.args.get("offset", 0, type=int), 0))
    tags = web_data.article_team_tags([a["id"] for a in rows])
    return respond([article_item(a, tags=tags.get(a["id"], [])) for a in rows], max_age=120)


def team_ids_arg():
    raw = request.args.get("team_id") or request.args.get("team_ids") or ""
    ids = [t for t in raw.replace(" ", "").split(",") if t]
    if any(not t.isdigit() for t in ids) or len(ids) > 50:
        raise ApiError(400, "bad_request", "team_id must be a comma-separated list of team ids (at most 50).")
    return ids


def news_item(i):
    out = {"id": i["id"], "source": i["source"], "kind": i["kind"], "headline": i["headline"], "summary": i["summary"],
           "published_at": iso(i["published_at"]), "teams": [team_tag(t) for t in i["teams"]]}
    if i["source"] == "sidelinewire":
        out.update(article_id=str(i["article_id"]), slug=i["slug"],
                   url=f"https://{request.host}/{request.view_args['league']}/news/{i['slug']}")
    else:
        out["url"] = i["url"]
    return out


@bp.get("/<league>/news")
def news(league):
    """Team news for the app: our stories plus ESPN headlines, each tagged with its teams. Sync with ?since=<the
    newest published_at you have>; filter with ?team_id=1,2,3 (a user's followed teams)."""
    league_or_error(league)
    since = request.args.get("since")
    if since:
        try:
            since = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(400, "bad_request", "since must be an ISO 8601 time, e.g. 2026-09-25T18:00:00Z.") from None
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    items = web_data.news_feed(league, team_ids_arg(), since, limit)
    return respond([news_item(i) for i in items], max_age=60,
                   newest=iso(items[0]["published_at"]) if items else None)


@bp.get("/<league>/teams/<team_id>/news")
def team_news_feed(league, team_id):
    league_or_error(league)
    team_or_error(league, team_id)
    limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
    return respond([news_item(i) for i in web_data.news_feed(league, [team_id], None, limit)], max_age=120)


@bp.get("/<league>/articles/<slug>")
def article(league, slug):
    league_or_error(league)
    a = web_data.article(slug=slug)
    if not a or a["league"] != league:
        raise ApiError(404, "not_found", "No such article.")
    return respond(article_item(a, body=True), max_age=300)


# --- push notification devices ---------------------------------------------------------------

INSTALL_ID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
APNS_TOKEN = re.compile(r"^[0-9A-Fa-f]{64,200}$")
ACTIVITY_TOKEN = re.compile(r"^[0-9A-Fa-f]{64,512}$")  # Live Activity push tokens are longer (128 bytes today)
BUNDLE_ID = re.compile(r"^[A-Za-z0-9.-]{3,155}$")
TEAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_FOLLOWS = 200
MAX_GAME_FOLLOWS = 50
ALERTS = ("kickoff", "scoring", "final", "news", "upset", "close", "soon")
LEAGUE_ALERTS = ("upset", "close", "news")  # push_league_alerts.alert_<name>; missing = off  # push_devices.alert_<name>; missing = on


def install_id_or_error(install_id):
    if not INSTALL_ID.match(install_id):
        raise ApiError(400, "bad_request", "The device id must be a UUID.")
    return install_id.lower()


def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.put("/devices/<install_id>")
def register_device(install_id):
    """Create or replace an install's registration: token, alert switches and followed teams, all at once,
    so the app can resend its whole state whenever anything changes."""
    install_id = install_id_or_error(install_id)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Send a JSON object.")
    token = str(body.get("apns_token") or "")
    if not APNS_TOKEN.match(token):
        raise ApiError(400, "bad_request", "apns_token must be the device token as hex.")
    environment = body.get("environment")
    if environment not in ("sandbox", "production"):
        raise ApiError(400, "bad_request", "environment must be sandbox or production.")
    bundle_id = str(body.get("bundle_id") or "")
    if not BUNDLE_ID.match(bundle_id):
        raise ApiError(400, "bad_request", "bundle_id is required.")
    timezone_name = body.get("timezone")
    timezone_name = str(timezone_name)[:64] if timezone_name else None
    alerts = body.get("alerts") or {}
    if not isinstance(alerts, dict):
        raise ApiError(400, "bad_request", "alerts must be an object of booleans.")
    switches = [alerts.get(name, True) is not False for name in ALERTS]
    # Auto-follow on the lock screen is opt-in, and needs the app's push-to-start token.
    auto_activities = alerts.get("live_activity") is True
    start_token = body.get("activity_start_token")
    if start_token is not None and not (isinstance(start_token, str) and ACTIVITY_TOKEN.match(start_token)):
        raise ApiError(400, "bad_request", "activity_start_token must be hex.")
    follows = body.get("follows") or []
    if not isinstance(follows, list) or len(follows) > MAX_FOLLOWS:
        raise ApiError(400, "bad_request", f"follows must be a list of at most {MAX_FOLLOWS} teams.")
    teams = {}  # (league, team_id) -> per-team overrides, None where the device setting applies
    for item in follows:
        league, team_id = (item or {}).get("league"), str((item or {}).get("team_id") or "")
        if league not in site.LEAGUES or not TEAM_ID.match(team_id):
            raise ApiError(400, "bad_request", "Each follow needs a league (nfl or cfb) and a team_id.")
        overrides = (item or {}).get("alerts") or {}
        if not isinstance(overrides, dict):
            raise ApiError(400, "bad_request", "A follow's alerts must be an object of booleans.")
        teams[(league, team_id)] = tuple(overrides[n] if isinstance(overrides.get(n), bool) else None for n in ALERTS)
    league_alerts = body.get("leagues") or {}
    if not isinstance(league_alerts, dict) or any(k not in site.LEAGUES or not isinstance(v, dict)
                                                   for k, v in league_alerts.items()):
        raise ApiError(400, "bad_request", "leagues must map nfl/cfb to an object of booleans.")
    league_rows = {lg: tuple(v.get(n) is True for n in LEAGUE_ALERTS) for lg, v in league_alerts.items()}
    # Single games from the + menu: alerts for the game and/or a Live Activity when it kicks off.
    game_follows = body.get("games") or []
    if not isinstance(game_follows, list) or len(game_follows) > MAX_GAME_FOLLOWS:
        raise ApiError(400, "bad_request", f"games must be a list of at most {MAX_GAME_FOLLOWS} games.")
    games = {}
    for item in game_follows:
        item = item if isinstance(item, dict) else {}
        league, game_id = item.get("league"), str(item.get("game_id") or "")
        if league not in site.LEAGUES or not game_id.isdigit():
            raise ApiError(400, "bad_request", "Each followed game needs a league (nfl or cfb) and a game_id.")
        wants = (item.get("alerts") is True, item.get("live_activity") is True)
        if any(wants):
            games[(league, game_id)] = wants
    # Mock betting: this install's player (for result alerts) and whether it wants them (default on).
    bet_player_id = optional_uuid(body.get("bet_player_id"))
    bet_alerts = alerts.get("bets") is not False

    try:
        with connect() as conn, conn.transaction():
            # A reinstall gets a new install id but can keep its token; the old row goes.
            conn.execute("DELETE FROM push_devices WHERE apns_token = %s AND install_id <> %s", (token, install_id))
            conn.execute(
                """
                INSERT INTO push_devices (install_id, apns_token, environment, bundle_id, timezone, alert_kickoff,
                                          alert_scoring, alert_final, alert_news, alert_upset, alert_close, alert_soon,
                                          api_key_prefix, auto_activities, activity_start_token, bet_player_id,
                                          alert_bets)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (install_id) DO UPDATE SET
                    apns_token = EXCLUDED.apns_token, environment = EXCLUDED.environment,
                    bundle_id = EXCLUDED.bundle_id, timezone = EXCLUDED.timezone,
                    alert_kickoff = EXCLUDED.alert_kickoff, alert_scoring = EXCLUDED.alert_scoring,
                    alert_final = EXCLUDED.alert_final, alert_news = EXCLUDED.alert_news,
                    alert_upset = EXCLUDED.alert_upset, alert_close = EXCLUDED.alert_close,
                    alert_soon = EXCLUDED.alert_soon,
                    api_key_prefix = EXCLUDED.api_key_prefix, auto_activities = EXCLUDED.auto_activities,
                    activity_start_token = EXCLUDED.activity_start_token,
                    bet_player_id = EXCLUDED.bet_player_id, alert_bets = EXCLUDED.alert_bets,
                    disabled_at = NULL, last_error = NULL, updated_at = now()
                """,
                (install_id, token, environment, bundle_id, timezone_name, *switches, g.get("api_key_prefix"),
                 auto_activities, start_token.lower() if start_token else None, bet_player_id, bet_alerts))
            conn.execute("DELETE FROM push_follows WHERE install_id = %s", (install_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO push_follows (install_id, league, team_id, alert_kickoff, alert_scoring, alert_final, "
                    "alert_news, alert_upset, alert_close, alert_soon) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(install_id, league, team_id, *overrides) for (league, team_id), overrides in sorted(teams.items())])
            conn.execute("DELETE FROM push_game_follows WHERE install_id = %s", (install_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO push_game_follows (install_id, league, game_id, alerts, live_activity) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(install_id, league, game_id, *wants) for (league, game_id), wants in sorted(games.items())])
            conn.execute("DELETE FROM push_league_alerts WHERE install_id = %s", (install_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO push_league_alerts (install_id, league, alert_upset, alert_close, alert_news) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(install_id, lg, *row) for lg, row in sorted(league_rows.items()) if any(row)])
    except psycopg.errors.UndefinedTable:
        # The tables are created by the pipeline's schema setup; until that has run, say so plainly.
        raise ApiError(503, "unavailable", "Notifications aren't set up on this server yet.")
    return no_store(respond({"install_id": install_id, "follows": len(teams), "games": len(games),
                             "alerts": {**dict(zip(ALERTS, switches)), "live_activity": auto_activities,
                                        "bets": bet_alerts},
                             "leagues": {lg: dict(zip(LEAGUE_ALERTS, row)) for lg, row in league_rows.items()}}))


@bp.get("/devices/<install_id>/alerts")
def device_alerts(install_id):
    """The alerts this install was sent, newest first (the app's alert history)."""
    install_id = install_id_or_error(install_id)
    limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
    try:
        rows = site.query("SELECT id, league, game_id, kind, title, body, slug, created_at FROM push_deliveries "
                          "WHERE install_id = %s ORDER BY created_at DESC LIMIT %s", (install_id, limit))
    except psycopg.errors.UndefinedTable:
        rows = []
    return no_store(respond([{"id": str(r["id"]), "league": r["league"], "game_id": r["game_id"], "kind": r["kind"],
                              "title": r["title"], "body": r["body"], "slug": r["slug"],
                              "sent_at": iso(r["created_at"])} for r in rows]))


@bp.delete("/devices/<install_id>")
def delete_device(install_id):
    """Forget an install (notifications turned off, or the app is signing out)."""
    install_id = install_id_or_error(install_id)
    try:
        with connect() as conn:
            deleted = conn.execute("DELETE FROM push_devices WHERE install_id = %s", (install_id,)).rowcount
    except psycopg.errors.UndefinedTable:
        deleted = 0
    return no_store(respond({"install_id": install_id, "deleted": bool(deleted)}))


# --- Live Activities -----------------------------------------------------------------------------

@bp.put("/live-activities/<token>")
def register_activity(token):
    """Register a Live Activity's push token for one game; notify.py keeps it updated until the final."""
    if not ACTIVITY_TOKEN.match(token):
        raise ApiError(400, "bad_request", "The activity token must be hex.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Send a JSON object.")
    league, game_id = body.get("league"), str(body.get("game_id") or "")
    if league not in site.LEAGUES or not TEAM_ID.match(game_id):
        raise ApiError(400, "bad_request", "league (nfl or cfb) and game_id are required.")
    environment = body.get("environment")
    if environment not in ("sandbox", "production"):
        raise ApiError(400, "bad_request", "environment must be sandbox or production.")
    bundle_id = str(body.get("bundle_id") or "")
    if not BUNDLE_ID.match(bundle_id):
        raise ApiError(400, "bad_request", "bundle_id is required.")
    install_id = body.get("install_id")
    install_id = install_id.lower() if isinstance(install_id, str) and INSTALL_ID.match(install_id) else None
    if not games_by(league, "g.game_id = %s", (game_id,)):
        raise ApiError(404, "not_found", f"No {league} game {game_id}.")
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO push_activities (token, league, game_id, environment, bundle_id, install_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE SET league = EXCLUDED.league, game_id = EXCLUDED.game_id,
                    environment = EXCLUDED.environment, bundle_id = EXCLUDED.bundle_id,
                    install_id = EXCLUDED.install_id, ended_at = NULL, last_error = NULL, updated_at = now()
                """, (token.lower(), league, game_id, environment, bundle_id, install_id))
    except psycopg.errors.UndefinedTable:
        raise ApiError(503, "unavailable", "Live Activities aren't set up on this server yet.")
    return no_store(respond({"token": token.lower(), "league": league, "game_id": game_id}))


@bp.delete("/live-activities/<token>")
def delete_activity(token):
    """The user ended the activity; stop pushing to it."""
    if not ACTIVITY_TOKEN.match(token):
        raise ApiError(400, "bad_request", "The activity token must be hex.")
    try:
        with connect() as conn:
            deleted = conn.execute("DELETE FROM push_activities WHERE token = %s", (token.lower(),)).rowcount
    except psycopg.errors.UndefinedTable:
        deleted = 0
    return no_store(respond({"token": token.lower(), "deleted": bool(deleted)}))


# --- accounts (Sign in with Apple) ---------------------------------------------------------------

def bearer_token():
    auth = request.headers.get("Authorization") or ""
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def current_user(conn):
    """The signed-in user's id; 401 signed_out when the session is missing or gone."""
    user_id = accounts.session_user(conn, bearer_token())
    if not user_id:
        raise ApiError(401, "signed_out", "Sign in to do that.")
    return user_id


def user_item(user):
    return {"user_id": user["user_id"], "display_name": user["display_name"], "email": user["email"],
            "created_at": iso(user["created_at"])}


def optional_uuid(value):
    value = str(value or "")
    return value.lower() if INSTALL_ID.match(value) else None


@bp.post("/auth/apple")
def auth_apple():
    """Exchange a Sign in with Apple identity token for a session. Links this device's betting player."""
    body = request.get_json(silent=True) or {}
    identity_token = str(body.get("identity_token") or "")
    if not identity_token:
        raise ApiError(400, "bad_request", "identity_token is required.")
    try:
        claims = accounts.verify_identity_token(identity_token)
    except accounts.AuthError as err:
        raise ApiError(401, "invalid_sign_in", str(err)) from None
    with connect() as conn, conn.transaction():
        token, user = accounts.sign_in(conn, claims, full_name=body.get("full_name"),
                                       authorization_code=body.get("authorization_code"),
                                       install_id=optional_uuid(body.get("install_id")))
        player_id = accounts.link_player(conn, user["user_id"], optional_uuid(body.get("player_id")))
    return no_store(respond({"token": token, "user": user_item(user), "player_id": player_id}))


@bp.get("/me")
def get_me():
    with connect() as conn:
        user_id = current_user(conn)
        player = conn.execute("SELECT player_id FROM bet_players WHERE user_id = %s", (user_id,)).fetchone()
        return no_store(respond({"user": user_item(accounts.user(conn, user_id)),
                                 "player_id": player[0] if player else None}))


@bp.put("/me")
def put_me():
    """Change the display name shown in chat."""
    name = accounts.clean_display_name((request.get_json(silent=True) or {}).get("display_name"))
    if not name or len(name) < 2:
        raise ApiError(400, "bad_request", f"Names are 2-{accounts.DISPLAY_NAME_MAX} characters.")
    with connect() as conn:
        user_id = current_user(conn)
        conn.execute("UPDATE users SET display_name = %s, updated_at = now() WHERE user_id = %s", (name, user_id))
        return no_store(respond({"user": user_item(accounts.user(conn, user_id))}))


@bp.post("/auth/signout")
def sign_out():
    with connect() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token_hash = %s", (accounts.hash_token(bearer_token()),))
    return no_store(respond({"signed_out": True}))


@bp.delete("/me")
def delete_me():
    """Delete the account and everything tied to it (betting player, chat); revokes Sign in with Apple."""
    with connect() as conn:
        user_id = current_user(conn)
        accounts.delete_user(conn, user_id)
    return no_store(respond({"deleted": True}))


# --- chat (private groups) -------------------------------------------------------------------------

@bp.errorhandler(chat.ChatError)
def chat_error(err):
    return api_error(ApiError(err.status, err.code, err.message))


def group_id_or_404(group_id):
    group_id = optional_uuid(group_id)
    if not group_id:
        raise ApiError(404, "not_found", "No such group.")
    return group_id


def int_or_404(value, what="message"):
    if not str(value).isdigit():
        raise ApiError(404, "not_found", f"No such {what}.")
    return int(value)


def group_item(g, me):
    last = None
    if g["last_id"] is not None:
        last = {"id": str(g["last_id"]), "body": g["last_body"], "display_name": g["last_name"],
                "is_me": g["last_user_id"] == me, "created_at": iso(g["last_at"])}
    return {"group_id": g["group_id"], "name": g["name"], "is_owner": g["owner_id"] == me,
            "invite_code": g["invite_code"], "invite_url": f"{SITE_URL}/join/{g['invite_code']}",
            "muted": g["muted"], "members": g["members"], "unread": g["unread"], "last_message": last,
            "created_at": iso(g["created_at"])}


def one_group(conn, group_id, me):
    group = next((g for g in chat.groups_for(conn, me) if g["group_id"] == group_id), None)
    if group is None:
        raise ApiError(404, "not_found", "No such group.")
    return group_item(group, me)


def shared_games(conn, messages):
    """Matchups for games shared in these messages, keyed by (league, game_id)."""
    out = {}
    for league, game_id in {(m["league"], m["game_id"]) for m in messages if m["game_id"]}:
        game, _ = betting.game_state(conn, league, game_id)
        if game:
            state = ("final" if game["completed"] or game["live_state"] == "post"
                     else "in" if game["live_state"] == "in" else "pre")
            out[(league, game_id)] = {"league": league, "id": game_id, "state": state, **bet_game(league, game)}
    return out


def message_item(m, me, games):
    deleted = m["deleted_at"] is not None
    return {"id": str(m["id"]), "user_id": m["user_id"], "display_name": m["display_name"], "is_me": m["user_id"] == me,
            "body": "" if deleted else m["body"], "deleted": deleted, "created_at": iso(m["created_at"]),
            "game": None if deleted else games.get((m["league"], m["game_id"]))}


@bp.get("/groups")
def list_groups():
    with connect() as conn:
        me = current_user(conn)
        return no_store(respond([group_item(g, me) for g in chat.groups_for(conn, me)]))


@bp.post("/groups")
def create_group():
    body = request.get_json(silent=True) or {}
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        group_id = chat.create_group(conn, me, body.get("name"))
        return no_store(respond(one_group(conn, group_id, me)))


@bp.post("/groups/join")
def join_group():
    body = request.get_json(silent=True) or {}
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        group_id = chat.join_group(conn, me, body.get("code"))
        return no_store(respond(one_group(conn, group_id, me)))


@bp.get("/groups/<group_id>")
def get_group(group_id):
    group_id = group_id_or_404(group_id)
    with connect() as conn:
        me = current_user(conn)
        group = one_group(conn, group_id, me)
        members = [{"user_id": m["user_id"], "display_name": m["display_name"], "is_me": m["user_id"] == me,
                    "is_owner": group["is_owner"] and m["user_id"] == me or None, "blocked": m["blocked"],
                    "joined_at": iso(m["joined_at"])} for m in chat.members_of(conn, group_id, me)]
        owner = conn.execute("SELECT owner_id FROM chat_groups WHERE group_id = %s", (group_id,)).fetchone()[0]
        for m in members:
            m["is_owner"] = m["user_id"] == owner
        return no_store(respond({"group": group, "members": members}))


@bp.put("/groups/<group_id>")
def update_group(group_id):
    """The owner renames the group; any member can mute it or mark it read (muted, last_read_id)."""
    group_id = group_id_or_404(group_id)
    body = request.get_json(silent=True) or {}
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        if "name" in body:
            chat.require_owner(conn, group_id, me)
            conn.execute("UPDATE chat_groups SET name = %s, updated_at = now() WHERE group_id = %s",
                         (chat.clean_name(body["name"]), group_id))
        if "muted" in body:
            chat.set_muted(conn, group_id, me, body["muted"])
        if "last_read_id" in body:
            chat.mark_read(conn, group_id, me, int_or_404(body["last_read_id"]))
        return no_store(respond(one_group(conn, group_id, me)))


@bp.delete("/groups/<group_id>")
def delete_group(group_id):
    group_id = group_id_or_404(group_id)
    with connect() as conn:
        me = current_user(conn)
        chat.require_owner(conn, group_id, me)
        conn.execute("DELETE FROM chat_groups WHERE group_id = %s", (group_id,))
    return no_store(respond({"group_id": group_id, "deleted": True}))


@bp.post("/groups/<group_id>/invite")
def rotate_invite(group_id):
    """New invite code (the old one stops working)."""
    group_id = group_id_or_404(group_id)
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        chat.rotate_code(conn, group_id, me)
        return no_store(respond(one_group(conn, group_id, me)))


@bp.delete("/groups/<group_id>/members/<member_id>")
def remove_member(group_id, member_id):
    """Leave the group (your own id) or, as its creator, remove a member."""
    group_id = group_id_or_404(group_id)
    member_id = optional_uuid(member_id) or ""
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        chat.remove_member(conn, group_id, me, member_id)
    return no_store(respond({"group_id": group_id, "removed": member_id}))


@bp.get("/groups/<group_id>/messages")
def group_messages(group_id):
    """Oldest first. ?after=<id> for new messages (polling), ?before=<id> for older ones, neither for the latest."""
    group_id = group_id_or_404(group_id)
    after, before = request.args.get("after"), request.args.get("before")
    after = int_or_404(after) if after else None
    before = int_or_404(before) if before else None
    limit = max(1, min(request.args.get("limit", 50, type=int), 100))
    with connect() as conn:
        me = current_user(conn)
        chat.membership(conn, group_id, me)
        rows = chat.messages(conn, group_id, me, after=after, before=before, limit=limit)
        games = shared_games(conn, rows)
        return no_store(respond([message_item(m, me, games) for m in rows]))


@bp.post("/groups/<group_id>/messages")
def post_message(group_id):
    """Send a message; optionally share a game with league and game_id."""
    group_id = group_id_or_404(group_id)
    body = request.get_json(silent=True) or {}
    league = body.get("league") if body.get("league") in site.LEAGUES else None
    game_id = str(body.get("game_id") or "") or None
    with connect() as conn, conn.transaction():
        me = current_user(conn)
        message = chat.post_message(conn, group_id, me, body.get("body"), league, game_id if league else None)
        return no_store(respond(message_item(message, me, shared_games(conn, [message]))))


@bp.delete("/groups/<group_id>/messages/<message_id>")
def delete_message(group_id, message_id):
    group_id, message_id = group_id_or_404(group_id), int_or_404(message_id)
    with connect() as conn:
        me = current_user(conn)
        chat.delete_message(conn, group_id, me, message_id)
    return no_store(respond({"id": str(message_id), "deleted": True}))


@bp.post("/groups/<group_id>/messages/<message_id>/report")
def report_message(group_id, message_id):
    group_id, message_id = group_id_or_404(group_id), int_or_404(message_id)
    reason = (request.get_json(silent=True) or {}).get("reason")
    with connect() as conn:
        me = current_user(conn)
        chat.report_message(conn, group_id, me, message_id, reason)
    return no_store(respond({"id": str(message_id), "reported": True}))


@bp.put("/users/<user_id>/block")
def block_user(user_id):
    with connect() as conn:
        me = current_user(conn)
        chat.block(conn, me, optional_uuid(user_id) or "", on=True)
    return no_store(respond({"user_id": user_id, "blocked": True}))


@bp.delete("/users/<user_id>/block")
def unblock_user(user_id):
    with connect() as conn:
        me = current_user(conn)
        chat.block(conn, me, optional_uuid(user_id) or "", on=False)
    return no_store(respond({"user_id": user_id, "blocked": False}))


# --- mock betting ("Beat the Model") --------------------------------------------------------------

NICKNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,18}[A-Za-z0-9]$")
RESERVED_NICKNAMES = {"sidelinewire", "model", "the model", "admin", "support"}


def player_or_error(conn, player_id):
    row = conn.execute("SELECT player_id, nickname, reset_at, resets FROM bet_players WHERE player_id = %s",
                       (player_id,)).fetchone()
    if not row:
        raise ApiError(404, "not_found", "No such player; create one with PUT /players/<id>.")
    return dict(zip(("player_id", "nickname", "reset_at", "resets"), row))


def bet_game(league, game):
    """The matchup a bet is on, so a bet list needs no per-game requests."""
    return {"start_time": iso(game["start_time"]), "completed": bool(game["completed"]),
            "home": {**team_ref(league, game["home_team_id"]), "score": game["home_score"]},
            "away": {**team_ref(league, game["away_team_id"]), "score": game["away_score"]}}


def bet_item(row, game):
    b = dict(zip(("id", "league", "game_id", "market", "selection", "line", "price", "stake", "model_selection",
                  "status", "profit", "placed_at", "settled_at"), row))
    return {"id": str(b["id"]), "league": b["league"], "game_id": b["game_id"], "game": bet_game(b["league"], game),
            "market": b["market"],
            "selection": b["selection"], "line": num(b["line"], 1), "price": b["price"], "stake": num(b["stake"], 2),
            "model_selection": b["model_selection"], "status": b["status"], "profit": num(b["profit"], 2),
            "model_result": betting.model_result(b["status"], b["selection"], b["model_selection"])
            if b["status"] != "open" else None,
            "to_win": num(betting.win_amount(b["stake"], b["price"]), 2),
            "placed_at": iso(b["placed_at"]), "settled_at": iso(b["settled_at"])}


BET_COLUMNS = ("id, league, game_id, market, selection, line, price, stake, model_selection, status, profit, "
               "placed_at, settled_at")
BET_GAME_KEYS = ("start_time", "completed", "home_team_id", "away_team_id", "home_score", "away_score")


def player_summary(conn, player):
    balance, available = betting.bankroll(conn, player["player_id"], player["reset_at"])
    season = conn.execute("SELECT max(season) FROM (SELECT season FROM bets WHERE player_id = %(p)s UNION ALL "
                          "SELECT season FROM parlays WHERE player_id = %(p)s) t", {"p": player["player_id"]}).fetchone()[0]
    # Parlays count toward the record and profit, not the with/against-the-model split.
    rows = lambda since: conn.execute(  # noqa: E731
        "SELECT status, stake, profit, selection, model_selection FROM ("
        "  SELECT status, stake, profit, selection, model_selection, season, placed_at FROM bets WHERE player_id = %(p)s"
        "  UNION ALL SELECT status, stake, profit, NULL, NULL, season, placed_at FROM parlays WHERE player_id = %(p)s) t "
        "WHERE (%(s)s::int IS NULL OR season = %(s)s) AND (%(since)s::timestamptz IS NULL OR placed_at >= %(since)s::timestamptz)",
        {"p": player["player_id"], "s": season, "since": since}).fetchall()
    return {"player_id": player["player_id"], "nickname": player["nickname"], "bankroll": betting.BANKROLL,
            "balance": balance, "available": available, "resets": player["resets"],
            "can_reset": available < betting.RESET_BELOW and balance == available,
            "season": betting.summarize(rows(None)), "week": betting.summarize(rows(betting.week_start()))}


@bp.put("/players/<player_id>")
def put_player(player_id):
    """Create a player or change its nickname."""
    player_id = install_id_or_error(player_id)
    nickname = str((request.get_json(silent=True) or {}).get("nickname") or "").strip()
    if not NICKNAME.match(nickname) or nickname.lower() in RESERVED_NICKNAMES:
        raise ApiError(400, "bad_request", "Nicknames are 3-20 letters, numbers, spaces, dots, dashes or underscores.")
    with connect() as conn:
        try:
            conn.execute("INSERT INTO bet_players (player_id, nickname) VALUES (%s, %s) ON CONFLICT (player_id) "
                         "DO UPDATE SET nickname = EXCLUDED.nickname, updated_at = now()", (player_id, nickname))
        except psycopg.errors.UniqueViolation:
            raise ApiError(409, "conflict", "That nickname is taken.") from None
        return no_store(respond(player_summary(conn, player_or_error(conn, player_id))))


@bp.get("/players/<player_id>")
def get_player(player_id):
    player_id = install_id_or_error(player_id)
    with connect() as conn:
        return no_store(respond(player_summary(conn, player_or_error(conn, player_id))))


@bp.post("/players/<player_id>/reset")
def reset_player(player_id):
    """Start over at the full bankroll; only when nearly broke with nothing open. Resets are counted."""
    player_id = install_id_or_error(player_id)
    with connect() as conn:
        player = player_or_error(conn, player_id)
        if not player_summary(conn, player)["can_reset"]:
            raise ApiError(409, "conflict", f"You can reset once you're under {betting.RESET_BELOW} units "
                                            "with no open bets.")
        conn.execute("UPDATE bet_players SET reset_at = now(), resets = resets + 1, updated_at = now() "
                     "WHERE player_id = %s", (player_id,))
        return no_store(respond(player_summary(conn, player_or_error(conn, player_id))))


@bp.get("/players/<player_id>/bets")
def player_bets(player_id):
    player_id = install_id_or_error(player_id)
    status = request.args.get("status")
    if status not in (None, "open", "settled"):
        raise ApiError(400, "bad_request", "status must be open or settled.")
    where = {"open": "AND status = 'open'", "settled": "AND status <> 'open'", None: ""}[status]
    with connect() as conn:
        player_or_error(conn, player_id)
        columns = ", ".join(f"b.{c.strip()}" for c in BET_COLUMNS.split(",")) + ", " + \
            ", ".join(f"g.{c}" for c in BET_GAME_KEYS)
        rows = conn.execute(f"SELECT {columns} FROM bets b JOIN games g USING (league, game_id) "
                            f"WHERE b.player_id = %s {where.replace('status', 'b.status')} "
                            "ORDER BY b.placed_at DESC LIMIT 200", (player_id,)).fetchall()
    width = len(BET_COLUMNS.split(","))
    return no_store(respond([bet_item(r[:width], dict(zip(BET_GAME_KEYS, r[width:]))) for r in rows]))


@bp.post("/players/<player_id>/bets")
def place_bet(player_id):
    """Place a bet at the current consensus price, which is locked into the bet."""
    player_id = install_id_or_error(player_id)
    body = request.get_json(silent=True) or {}
    league, game_id = body.get("league"), str(body.get("game_id") or "")
    market, selection = body.get("market"), body.get("selection")
    league_or_error(league)
    if market not in betting.MARKETS or selection not in betting.MARKETS[market]:
        raise ApiError(400, "bad_request", "market is spread, moneyline or total; selection is home/away or over/under.")
    try:
        stake = round(float(body.get("stake")), 2)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "stake must be a number of units.") from None
    if not betting.MIN_STAKE <= stake <= betting.MAX_STAKE:
        raise ApiError(400, "bad_request", f"Stakes are {betting.MIN_STAKE}-{betting.MAX_STAKE} units.")
    with connect() as conn, conn.transaction():
        player = player_or_error(conn, player_id)
        conn.execute("SELECT 1 FROM bet_players WHERE player_id = %s FOR UPDATE", (player_id,))  # one bet at a time
        prices = betting.markets(conn, league, game_id)
        if prices is None:
            raise ApiError(404, "not_found", f"No {league} game {game_id}.")
        if prices["locked"]:
            raise ApiError(409, "locked", "Betting on this game closed at kickoff.")
        q = betting.quote(prices, market, selection)
        if q is None:
            raise ApiError(409, "unavailable", f"There's no {market} line for this game yet.")
        _, available = betting.bankroll(conn, player_id, player["reset_at"])
        if stake > available:
            raise ApiError(409, "insufficient", f"You have {available:g} units available.")
        game, _ = betting.game_state(conn, league, game_id)
        try:
            row = conn.execute(
                f"INSERT INTO bets (player_id, league, game_id, season, market, selection, line, price, stake, "
                f"model_selection) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {BET_COLUMNS}",
                (player_id, league, game_id, game["season"], market, selection, q[0], q[1], stake,
                 prices["model"][market])).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ApiError(409, "conflict", f"You already have a {market} bet on this game.") from None
    return no_store(respond(bet_item(row, game)))


@bp.delete("/players/<player_id>/bets/<bet_id>")
def cancel_bet(player_id, bet_id):
    """Cancel an open bet before kickoff; the stake returns to the bankroll."""
    player_id = install_id_or_error(player_id)
    if not bet_id.isdigit():
        raise ApiError(400, "bad_request", "Unknown bet.")
    with connect() as conn:
        row = conn.execute("SELECT league, game_id FROM bets WHERE id = %s AND player_id = %s AND status = 'open'",
                           (int(bet_id), player_id)).fetchone()
        if not row:
            raise ApiError(404, "not_found", "No such open bet.")
        if betting.game_state(conn, row[0], row[1])[1]:
            raise ApiError(409, "locked", "Bets can't be cancelled after kickoff.")
        conn.execute("DELETE FROM bets WHERE id = %s AND status = 'open'", (int(bet_id),))
    return no_store(respond({"id": bet_id, "cancelled": True}))


PARLAY_COLUMNS = "id, stake, odds, status, profit, placed_at, settled_at"
LEG_COLUMNS = ("l.parlay_id, l.league, l.game_id, l.market, l.selection, l.line, l.price, l.model_selection, l.status, "
               "g.start_time, g.completed, g.home_team_id, g.away_team_id, g.home_score, g.away_score")


def parlay_items(conn, rows):
    """Parlays with their legs (and each leg's matchup), in the order given."""
    parlays = [dict(zip(("id", "stake", "odds", "status", "profit", "placed_at", "settled_at"), r)) for r in rows]
    legs = {}
    if parlays:
        for r in conn.execute(f"SELECT {LEG_COLUMNS} FROM parlay_legs l JOIN games g USING (league, game_id) "
                              "WHERE l.parlay_id = ANY(%s) ORDER BY g.start_time, l.id", ([p["id"] for p in parlays],)):
            leg = dict(zip(("parlay_id", "league", "game_id", "market", "selection", "line", "price", "model_selection",
                            "status", *BET_GAME_KEYS), r))
            legs.setdefault(leg["parlay_id"], []).append({
                "league": leg["league"], "game_id": leg["game_id"],
                "game": bet_game(leg["league"], {k: leg[k] for k in BET_GAME_KEYS}),
                "market": leg["market"], "selection": leg["selection"], "line": num(leg["line"], 1),
                "price": leg["price"], "model_selection": leg["model_selection"], "status": leg["status"]})
    out = []
    for p in parlays:
        odds = float(p["odds"])
        out.append({"id": str(p["id"]), "stake": num(p["stake"], 2), "odds": num(odds, 4), "price": betting.american(odds),
                    "to_win": num(float(p["stake"]) * odds - float(p["stake"]), 2), "status": p["status"],
                    "profit": num(p["profit"], 2), "placed_at": iso(p["placed_at"]), "settled_at": iso(p["settled_at"]),
                    "legs": legs.get(p["id"], [])})
    return out


@bp.get("/players/<player_id>/parlays")
def player_parlays(player_id):
    player_id = install_id_or_error(player_id)
    status = request.args.get("status")
    if status not in (None, "open", "settled"):
        raise ApiError(400, "bad_request", "status must be open or settled.")
    where = {"open": "AND status = 'open'", "settled": "AND status <> 'open'", None: ""}[status]
    with connect() as conn:
        player_or_error(conn, player_id)
        rows = conn.execute(f"SELECT {PARLAY_COLUMNS} FROM parlays WHERE player_id = %s {where} "
                            "ORDER BY placed_at DESC LIMIT 100", (player_id,)).fetchall()
        return no_store(respond(parlay_items(conn, rows)))


@bp.post("/players/<player_id>/parlays")
def place_parlay(player_id):
    """One stake on 2-6 legs from different games, each at its current consensus price (locked in)."""
    player_id = install_id_or_error(player_id)
    body = request.get_json(silent=True) or {}
    legs = body.get("legs")
    if not isinstance(legs, list) or not betting.MIN_LEGS <= len(legs) <= betting.MAX_LEGS:
        raise ApiError(400, "bad_request", f"A parlay has {betting.MIN_LEGS}-{betting.MAX_LEGS} legs.")
    try:
        stake = round(float(body.get("stake")), 2)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "stake must be a number of units.") from None
    if not betting.MIN_STAKE <= stake <= betting.MAX_STAKE:
        raise ApiError(400, "bad_request", f"Stakes are {betting.MIN_STAKE}-{betting.MAX_STAKE} units.")
    games = set()
    for leg in legs:
        leg = leg if isinstance(leg, dict) else {}
        league_or_error(leg.get("league"))
        if leg.get("market") not in betting.MARKETS or leg.get("selection") not in betting.MARKETS[leg["market"]]:
            raise ApiError(400, "bad_request", "Each leg needs a market (spread, moneyline, total) and a selection.")
        key = (leg["league"], str(leg.get("game_id") or ""))
        if key in games:
            raise ApiError(400, "bad_request", "A parlay can have only one leg per game.")
        games.add(key)
    with connect() as conn, conn.transaction():
        player = player_or_error(conn, player_id)
        conn.execute("SELECT 1 FROM bet_players WHERE player_id = %s FOR UPDATE", (player_id,))
        placed, season = [], None
        for leg in legs:
            league, game_id = leg["league"], str(leg["game_id"])
            prices = betting.markets(conn, league, game_id)
            if prices is None:
                raise ApiError(404, "not_found", f"No {league} game {game_id}.")
            game, locked = betting.game_state(conn, league, game_id)
            matchup = f"{team_ref(league, game['away_team_id'])['abbreviation']} @ {team_ref(league, game['home_team_id'])['abbreviation']}"
            if locked:
                raise ApiError(409, "locked", f"Betting on {matchup} closed at kickoff. Remove that leg.")
            q = betting.quote(prices, leg["market"], leg["selection"])
            if q is None:
                raise ApiError(409, "unavailable", f"There's no {leg['market']} line for {matchup} right now.")
            placed.append((league, game_id, leg["market"], leg["selection"], q[0], q[1], prices["model"][leg["market"]]))
            season = max(season or 0, game["season"])
        odds = betting.parlay_odds([p[5] for p in placed])
        if odds > betting.MAX_PARLAY_ODDS:
            raise ApiError(409, "too_long", f"Parlay odds are capped at +{betting.american(betting.MAX_PARLAY_ODDS)}.")
        _, available = betting.bankroll(conn, player_id, player["reset_at"])
        if stake > available:
            raise ApiError(409, "insufficient", f"You have {available:g} units available.")
        row = conn.execute(f"INSERT INTO parlays (player_id, season, stake, odds) VALUES (%s, %s, %s, %s) "
                           f"RETURNING {PARLAY_COLUMNS}", (player_id, season, stake, odds)).fetchone()
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO parlay_legs (parlay_id, league, game_id, market, selection, line, price, "
                            "model_selection) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [(row[0], *p) for p in placed])
        return no_store(respond(parlay_items(conn, [row])[0]))


@bp.delete("/players/<player_id>/parlays/<parlay_id>")
def cancel_parlay(player_id, parlay_id):
    """Cancel an open parlay while none of its games has kicked off; the stake returns to the bankroll."""
    player_id = install_id_or_error(player_id)
    if not parlay_id.isdigit():
        raise ApiError(400, "bad_request", "Unknown parlay.")
    with connect() as conn:
        legs = conn.execute("SELECT l.league, l.game_id FROM parlays p JOIN parlay_legs l ON l.parlay_id = p.id "
                            "WHERE p.id = %s AND p.player_id = %s AND p.status = 'open'",
                            (int(parlay_id), player_id)).fetchall()
        if not legs:
            raise ApiError(404, "not_found", "No such open parlay.")
        if any(betting.game_state(conn, league, game_id)[1] for league, game_id in legs):
            raise ApiError(409, "locked", "Parlays can't be cancelled once one of their games has kicked off.")
        conn.execute("DELETE FROM parlays WHERE id = %s AND status = 'open'", (int(parlay_id),))
    return no_store(respond({"id": parlay_id, "cancelled": True}))


@bp.get("/<league>/games/<game_id>/markets")
def game_markets(league, game_id):
    """Prices a bet would lock in right now, the model's side of each market, and whether betting is closed."""
    league_or_error(league)
    with connect() as conn:
        prices = betting.markets(conn, league, game_id)
    if prices is None:
        raise ApiError(404, "not_found", f"No {league} game {game_id}.")
    return no_store(respond(prices))


@bp.get("/leaderboard")
def leaderboard():
    """Players ranked by profit (bets and parlays) this week or season, with the model's own record."""
    period = request.args.get("period", "week")
    if period not in ("week", "season"):
        raise ApiError(400, "bad_request", "period must be week or season.")
    me = request.args.get("player_id")
    since = betting.week_start() if period == "week" else None
    with connect() as conn:
        season = conn.execute("SELECT max(season) FROM bets").fetchone()[0] or datetime.now(timezone.utc).year
        rows = conn.execute(
            """
            SELECT p.player_id, p.nickname, count(*) FILTER (WHERE b.status = 'won'),
                   count(*) FILTER (WHERE b.status = 'lost'), count(*) FILTER (WHERE b.status = 'push'),
                   COALESCE(sum(b.profit), 0), COALESCE(sum(b.stake), 0)
            FROM (SELECT player_id, status, profit, stake, season, placed_at FROM bets
                  UNION ALL SELECT player_id, status, profit, stake, season, placed_at FROM parlays) b
            JOIN bet_players p USING (player_id)
            WHERE b.status <> 'open' AND b.season = %s AND (%s::timestamptz IS NULL OR b.placed_at >= %s::timestamptz)
            GROUP BY p.player_id, p.nickname ORDER BY 6 DESC, 3 DESC
            """, (season, since, since)).fetchall()
        model = web_data.cached(("bet_model", period, season, since and since.isoformat()),
                                lambda: betting.model_record(conn, season, since))
    entries = [{"rank": i + 1, "nickname": nick, "won": w, "lost": lo, "push": pu, "profit": num(profit, 2),
                "roi": num(float(profit) / float(staked), 4) if staked else None, "is_me": pid == me}
               for i, (pid, nick, w, lo, pu, profit, staked) in enumerate(rows)]
    mine = next((e for e in entries if e["is_me"]), None)
    return no_store(respond({"period": period, "season": season, "entries": entries[:50], "me": mine,
                             "model": model, "players": len(entries)}))


@bp.route("/<path:unused>")
def not_found(unused):
    raise ApiError(404, "not_found", "No such endpoint; see /api/v1/openapi.json.")
