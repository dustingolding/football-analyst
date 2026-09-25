"""OpenAPI 3.1 description of the JSON API (api.py), served at /api/v1/openapi.json.

Generate a Swift client with Apple's swift-openapi-generator from this document.
Keep it in step with api.py: every endpoint, parameter and response field is listed here.
"""


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}
NS, NI, NN, NB = nullable(S), nullable(I), nullable(N), nullable(B)


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": required or list(properties)}


def arr(items):
    return {"type": "array", "items": items}


def envelope(data, meta_extra=None):
    return obj({"data": data, "meta": obj({"generated_at": {"type": "string", "format": "date-time"},
                                           **(meta_extra or {})}, ["generated_at"])})


def param(name, where="query", schema=None, required=False, description=""):
    return {"name": name, "in": where, "required": required or where == "path", "schema": schema or S,
            "description": description}


LEAGUE = param("league", "path", {"type": "string", "enum": ["nfl", "cfb"]}, description="nfl or cfb")
GAME_ID = param("game_id", "path", description="ESPN event id")
TEAM_ID = param("team_id", "path", description="ESPN team id")
SEASON = param("season", schema=I, description="Season year (default: current)")
CONTRIBUTIONS = {"type": "object", "additionalProperties": N,
                 "description": "Points vs an average team by group: history, recruiting (NFL: draft), returning, "
                                "transfers (NFL: free agency), coaching"}

SCHEMAS = {
    "Error": obj({"error": obj({"code": S, "message": S})}),
    "TeamRef": obj({"id": S, "name": NS, "short_name": NS, "abbreviation": NS, "logo": NS, "color": NS, "rank": NI}),
    "Side": {"allOf": [ref("TeamRef"), obj({"score": NI})]},
    "LiveState": obj({
        "state": {"type": "string", "enum": ["pre", "in", "final"]}, "detail": NS, "home_score": NI, "away_score": NI,
        "possession_team_id": NS, "down_distance": NS, "red_zone": NB, "home_win_prob": NN, "last_play": NS,
        "updated_at": nullable({"type": "string", "format": "date-time"})}),
    "Game": obj({
        "id": S, "league": S, "season": I, "season_type": I, "week": NI,
        "start_time": nullable({"type": "string", "format": "date-time"}), "neutral_site": B, "notes": NS, "venue": NS,
        "status": obj({"state": {"type": "string", "enum": ["pre", "in", "final"]}, "detail": NS}),
        "home": ref("Side"), "away": ref("Side"),
        "line": obj({"home_spread": NN, "spread_text": NS, "total": NN, "home_win_prob": NN}),
        "prediction": obj({
            "home_win_prob": NN,
            "projected_score": nullable(obj({"home": NN, "away": NN})),
            "lean": nullable(obj({"team_id": S, "points": N, "model": S},)),
        }),
        "live": nullable(ref("LiveState")),
    }),
    "GameDetail": {"allOf": [ref("Game"), obj({
        "models": arr(obj({"model": S, "label": S, "home_win_prob": NN, "home_margin": NN, "total": NN})),
        "matchup": obj({"home": {"type": "object", "additionalProperties": NN},
                        "away": {"type": "object", "additionalProperties": NN}}),
        "sportsbooks": arr(obj({"source": S, "provider": S, "home_spread": NN, "total": NN, "home_moneyline": NI,
                                "away_moneyline": NI, "opening_home_spread": NN})),
        "injuries": obj({side: arr(ref("Injury")) for side in ("home", "away")}),
        "box_score": nullable(ref("BoxScore")),
    })]},
    "BoxScoreSide": obj({"players": arr(obj({"id": S, "name": NS, "stats": arr(S)})), "totals": nullable(arr(S))}),
    "BoxScore": obj({"final": B, "categories": arr(obj({
        "name": {"type": "string", "description": "passing, rushing, receiving, defensive, interceptions, fumbles, "
                                                  "kickReturns, puntReturns, kicking, punting"},
        "title": S, "labels": arr(S), "home": nullable(ref("BoxScoreSide")), "away": nullable(ref("BoxScoreSide"))}))}),
    "Injury": obj({"player": S, "position": NS, "status": S, "games": NI, "detail": NS, "url": NS}),
    "Play": obj({"id": S, "sequence": NI, "drive": NI, "period": NI, "clock": NS, "team_id": NS, "type": NS, "text": NS,
                 "home_score": NI, "away_score": NI, "scoring": B, "home_win_prob": NN}),
    "GameLive": obj({"game": ref("Game"), "plays": arr(ref("Play")),
                     "win_probability": arr(obj({"sequence": NI, "period": NI, "clock": NS, "home_win_prob": N}))}),
    "ScoreboardBlock": obj({"league": S, "games": arr(obj({
        "id": S, "start_time": nullable({"type": "string", "format": "date-time"}),
        "state": {"type": "string", "enum": ["pre", "in", "final"]}, "detail": NS, "broadcast": NS,
        "possession_team_id": NS, "down_distance": NS, "red_zone": NB,
        "home": {"allOf": [ref("Side"), obj({"record": NS})]}, "away": {"allOf": [ref("Side"), obj({"record": NS})]}}))}),
    "TeamListItem": {"allOf": [ref("TeamRef"), obj({"group": S, "record": NS, "elo_rank": NI})]},
    "TeamDetail": {"allOf": [ref("TeamRef"), obj({
        "season": I, "conference": NS, "division": NS,
        "record": obj({"overall": NS, "conference": NS, "division": NS, "streak": NS, "points_for": NI,
                       "points_against": NI}),
        "ratings": obj({"elo": NN, "elo_rank": NI, "off_epa": NN, "def_epa": NN, "qb_rating": NN, "form": NN}),
        "season_stats": {"type": "object", "additionalProperties": NN},
        "preseason": nullable(obj({"rating": NN, "rank": NI, "last_season": NN, "actual": NN, "elite_prob": NN,
                                   "contributions": CONTRIBUTIONS})),
        "next_game": nullable(ref("Game")),
    })]},
    "ScheduleGame": {"allOf": [ref("Game"), obj({"is_home": B, "opponent_id": S, "result": NS, "team_spread": NN,
                                                 "team_win_prob": NN, "covered": NB})]},
    "RosterPlayer": obj({"player_id": S, "name": S, "position": NS, "jersey": NI, "height_in": NI, "weight_lb": NI,
                         "experience": NS, "origin": NS, "headshot": NS, "unit": S}),
    "PlayerStatLine": {"type": "object", "properties": {"name": S, "position": NS, "games": I},
                       "additionalProperties": NN, "required": ["name", "games"]},
    "StandingsGroup": obj({"group": S, "teams": arr({"allOf": [ref("TeamRef"), obj({
        "overall": S, "conference": S, "division": S, "win_pct": N, "points_for": I, "points_against": I,
        "streak": S})]})}),
    "Poll": obj({"poll": S, "entries": arr({"allOf": [ref("TeamRef"), obj({
        "points": NI, "first_place_votes": NI, "record": NS, "movement": NI, "new": B})]})}),
    "Rating": {"allOf": [ref("TeamRef"), obj({"elo": N, "off_epa": NN, "def_epa": NN, "qb_rating": NN, "form": NN})]},
    "PreseasonRating": {"allOf": [ref("TeamRef"), obj({
        "rating": N, "last_season": NN, "actual": NN, "elite_prob": NN, "contributions": CONTRIBUTIONS,
        "transfers_in": I, "transfers_out": I})]},
    "LeaderBoard": obj({"category": S, "title": S, "entries": arr(obj({
        "name": NS, "position": NS, "team_id": S, "games": I, "value": S}))}),
    "Article": obj({"id": S, "slug": S, "league": S, "kind": {"type": "string", "enum": ["preview", "recap", "editorial"]},
                    "game_id": NS, "headline": S, "dek": NS, "published_at": nullable({"type": "string", "format": "date-time"}),
                    "url": S}),
    "ArticleDetail": {"allOf": [ref("Article"), obj({"paragraphs": arr(S)})]},
    "ModelMetrics": obj({"model": S, "description": S, "log_loss": NN, "brier": NN, "accuracy": NN, "margin_mae": NN,
                         "total_mae": NN, "ats": NN}),
}


def get(summary, data, params=(), meta=None, description=""):
    return {"get": {
        "summary": summary, "description": description, "parameters": list(params),
        "responses": {
            "200": {"description": "OK", "content": {"application/json": {"schema": envelope(data, meta)}}},
            "401": {"description": "Missing or invalid API key", "content": {"application/json": {"schema": ref("Error")}}},
            "404": {"description": "Not found", "content": {"application/json": {"schema": ref("Error")}}},
            "429": {"description": "Rate limited", "content": {"application/json": {"schema": ref("Error")}}},
        }}}


SEASON_META = {"season": NI}
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "SidelineWire API", "version": "1.0.0",
             "description": "Read-only NFL and college football data: games with predictions and live state, teams, "
                            "standings, rankings, ratings and leaders. Send your key in the X-API-Key header."},
    "servers": [{"url": "https://sidelinewire.com/api/v1"}, {"url": "https://dev.sidelinewire.com/api/v1"}],
    "security": [{"apiKey": []}],
    "components": {"schemas": SCHEMAS,
                   "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    "paths": {
        "/status": get("Current week per league", obj({
            "site_env": S, "leagues": arr(S),
            "current": {"type": "object", "additionalProperties": obj({"season": I, "season_type": I, "week": NI})}})),
        "/scoreboard": get("Scoreboard strip (live, upcoming, recent finals)", arr(ref("ScoreboardBlock")),
                           description="Poll every 30 s while games are live."),
        "/{league}/games": get("A week's games", arr(ref("Game")), [
            LEAGUE, SEASON, param("season_type", schema=I, description="2 regular, 3 postseason"),
            param("week", schema=I)], {"season": I, "season_type": I, "week": NI}),
        "/{league}/games/{game_id}": get("Game detail", ref("GameDetail"), [LEAGUE, GAME_ID]),
        "/{league}/games/{game_id}/live": get("Live state, plays and win probability", ref("GameLive"),
                                              [LEAGUE, GAME_ID, param("limit", schema=I, description="Plays, max 200")],
                                              description="Poll every 30-60 s during a game."),
        "/{league}/teams": get("Teams", arr(ref("TeamListItem")), [LEAGUE], SEASON_META),
        "/{league}/teams/{team_id}": get("Team detail", ref("TeamDetail"), [LEAGUE, TEAM_ID, SEASON]),
        "/{league}/teams/{team_id}/schedule": get("Team schedule", arr(ref("ScheduleGame")),
                                                  [LEAGUE, TEAM_ID, SEASON], SEASON_META),
        "/{league}/teams/{team_id}/roster": get("Team roster", arr(ref("RosterPlayer")), [LEAGUE, TEAM_ID, SEASON],
                                                SEASON_META),
        "/{league}/teams/{team_id}/stats": get("Team player stats by category",
                                               {"type": "object", "additionalProperties": arr(ref("PlayerStatLine"))},
                                               [LEAGUE, TEAM_ID, SEASON], SEASON_META),
        "/{league}/standings": get("Standings", arr(ref("StandingsGroup")), [LEAGUE, SEASON], SEASON_META),
        "/{league}/rankings": get("Polls (college only)", arr(ref("Poll")), [
            LEAGUE, SEASON, param("season_type", schema=I), param("week", schema=I)],
            {"season": NI, "season_type": NI, "week": NI}),
        "/{league}/ratings": get("Power ratings", arr(ref("Rating")), [LEAGUE], SEASON_META),
        "/{league}/preseason": get("Preseason ratings", arr(ref("PreseasonRating")), [LEAGUE, SEASON], SEASON_META),
        "/{league}/leaders": get("Stat leaders", arr(ref("LeaderBoard")), [
            LEAGUE, SEASON, param("type", schema={"type": "string", "enum": ["player", "team"]}),
            param("group", description="Conference (CFB) or conference/division (NFL)")],
            {"season": NI, "type": S, "group": NS}),
        "/{league}/articles": get("Published stories, newest first", arr(ref("Article")), [
            LEAGUE, param("kind", schema={"type": "string", "enum": ["preview", "recap", "editorial"]}),
            param("limit", schema=I, description="1-50, default 20"), param("offset", schema=I)],
            description="Written by our AI newsroom from game data and fact-checked before publishing."),
        "/{league}/articles/{slug}": get("One story, as plain paragraphs", ref("ArticleDetail"),
                                         [LEAGUE, param("slug", "path")]),
        "/models": get("Model performance on test seasons", {"type": "object", "additionalProperties": obj({
            "games": I, "test_first_season": I, "models": arr(ref("ModelMetrics"))})}),
    },
}
