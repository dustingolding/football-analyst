"""CFB box scores, rosters, polls and conference membership from CollegeFootballData.

    /games/players   per-player box scores (one request per week)
    /games/teams     per-team box scores (one request per week)
    /roster          rosters (one request per season)
    /rankings        AP, Coaches and CFP polls (one request per season)
    /teams           conference membership (already stored by cfbd_seasons.py)

Box score and roster responses are large, so they're cached gzipped under data/cfbd/ like
the play-by-play; polls go to raw_payloads. Stored responses are reused except for the
current season. CFBD game and athlete ids are ESPN's, so rows join onto games directly;
box-score teams are matched through the game's home/away side.

    python cfbd_box.py                     # 2005 through the current season
    python cfbd_box.py --start 2026        # just refresh this season
    python cfbd_box.py --no-fetch          # rebuild tables from the cache
"""

import argparse
import gzip
import json
import os
import time
from pathlib import Path

import requests
from psycopg.types.json import Jsonb

from backfill import current_season
from cfbd_seasons import team_ids
from database import connect, get_raw_payload, init_db, save_raw_payload

BASE_URL = "https://api.collegefootballdata.com"
CACHE_DIR = Path(__file__).parent / "data" / "cfbd"
SOURCE = "box"
POLLS = {"AP Top 25", "Coaches Poll", "Playoff Committee Rankings"}
SEASON_TYPES = {2: "regular", 3: "postseason"}

# CFBD category -> {CFBD stat type: our stat name}; "a/b" stats split into two names.
PLAYER_STATS = {
    "passing": {"C/ATT": ("pass_cmp", "pass_att"), "YDS": "pass_yds", "TD": "pass_td", "INT": "pass_int", "QBR": "qbr"},
    "rushing": {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td", "LONG": "rush_long"},
    "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td", "LONG": "rec_long"},
    "defensive": {"TOT": "tackles", "SOLO": "solo", "TFL": "tfl", "SACKS": "sacks", "PD": "pass_def",
                  "QB HUR": "qb_hurries", "TD": "def_td"},
    "interceptions": {"INT": "def_int", "YDS": "int_yds", "TD": "int_td"},
    "fumbles": {"FUM": "fumbles", "LOST": "fumbles_lost", "REC": "fumbles_rec"},
    "kicking": {"FG": ("fgm", "fga"), "XP": ("xpm", "xpa"), "LONG": "fg_long", "PTS": "kick_pts"},
    "punting": {"NO": "punts", "YDS": "punt_yds", "In 20": "punts_in20"},
    "kickReturns": {"NO": "kr", "YDS": "kr_yds", "TD": "kr_td"},
    "puntReturns": {"NO": "pr", "YDS": "pr_yds", "TD": "pr_td"},
}
TEAM_STATS = {
    "totalYards": "total_yds", "netPassingYards": "pass_yds", "rushingYards": "rush_yds",
    "rushingAttempts": "rush_att", "completionAttempts": ("pass_cmp", "pass_att"), "passingTDs": "pass_td",
    "rushingTDs": "rush_td", "firstDowns": "first_downs", "thirdDownEff": ("third_conv", "third_att"),
    "fourthDownEff": ("fourth_conv", "fourth_att"), "turnovers": "turnovers", "fumblesLost": "fumbles_lost",
    "interceptions": "def_int", "passesIntercepted": "ints_thrown", "fumblesRecovered": "fumbles_rec",
    "sacks": "sacks", "tacklesForLoss": "tfl", "totalPenaltiesYards": ("penalties", "penalty_yds"),
    "possessionTime": "possession_sec", "defensiveTDs": "def_td",
}


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def put(stats, name, raw):
    """Store raw under name; a (made, att) pair of names splits 'a/b' or 'a-b'."""
    if isinstance(name, tuple):
        text = str(raw).replace("-", "/")
        if "/" in text:
            a, b = text.split("/", 1)
            stats[name[0]], stats[name[1]] = number(a), number(b)
        return
    if name == "possession_sec" and ":" in str(raw):
        minutes, seconds = str(raw).split(":")
        stats[name] = number(minutes) * 60 + number(seconds)
        return
    value = number(raw)
    if value is not None:
        stats[name] = value


class Client:
    def __init__(self, pause):
        key = os.getenv("CFBD_API_KEY")
        if not key:
            raise SystemExit("Set CFBD_API_KEY in .env (free key: https://collegefootballdata.com/key).")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {key}"
        self.pause = pause
        self.calls = 0

    def get(self, endpoint, params):
        for attempt in range(5):
            response = self.session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=(10, 180))
            if response.status_code != 429:
                break
            time.sleep(10 * (attempt + 1))
        response.raise_for_status()
        self.calls += 1
        time.sleep(self.pause)
        return response.json()


def cached(client, endpoint, params, refresh):
    name = "-".join(str(v) for v in params.values())
    path = CACHE_DIR / endpoint.replace("/", "_") / f"{name}.json.gz"
    if path.exists() and not refresh:
        with gzip.open(path, "rt") as f:
            return json.load(f)
    if client is None:
        return None
    data = client.get(endpoint, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(data, f)
    return data


def fetch(conn, start, end, pause):
    client = Client(pause)
    weeks = conn.execute(
        "SELECT DISTINCT season, season_type, week FROM games WHERE league = 'cfb' AND season BETWEEN %s AND %s "
        "AND season_type IN (2, 3) AND week IS NOT NULL ORDER BY 1, 2, 3",
        (start, end),
    ).fetchall()
    for season, season_type, week in weeks:
        refresh = season >= current_season()
        params = {"year": season, "seasonType": SEASON_TYPES[season_type], "week": week}
        cached(client, "games/players", params, refresh)
        cached(client, "games/teams", params, refresh)
    for season in range(start, end + 1):
        refresh = season >= current_season()
        cached(client, "roster", {"year": season}, refresh)
        if refresh or get_raw_payload(conn, "cfb", "rankings", {"year": season}, "cfbd") is None:
            save_raw_payload(conn, "cfb", "rankings", {"year": season}, 200, client.get("rankings", {"year": season}),
                             source="cfbd")
    print(f"[cfbd_box] {client.calls} API calls", flush=True)


def load_box(conn):
    sides = {gid: {"home": h, "away": a} for gid, h, a in conn.execute(
        "SELECT game_id, home_team_id, away_team_id FROM games WHERE league = 'cfb'")}
    player_rows, team_rows = {}, {}
    for path in sorted((CACHE_DIR / "games_players").glob("*.json.gz")):
        with gzip.open(path, "rt") as f:
            games = json.load(f)
        for game in games:
            game_id = str(game["id"])
            if game_id not in sides:
                continue
            for team in game["teams"]:
                team_id = sides[game_id].get(team.get("homeAway"))
                if team_id is None:
                    continue
                players = {}
                for category in team.get("categories") or []:
                    names = PLAYER_STATS.get(category["name"])
                    if not names:
                        continue
                    for stat_type in category.get("types") or []:
                        name = names.get(stat_type["name"])
                        if not name:
                            continue
                        for athlete in stat_type.get("athletes") or []:
                            if not athlete.get("id") or str(athlete["id"]).startswith("-"):
                                continue  # team-level totals use negative ids
                            entry = players.setdefault(str(athlete["id"]), {"name": athlete.get("name"), "stats": {}})
                            put(entry["stats"], name, athlete.get("stat"))
                for player_id, entry in players.items():
                    player_rows[(game_id, player_id)] = ("cfb", game_id, player_id, team_id, SOURCE, entry["name"],
                                                         Jsonb(entry["stats"]))
    for path in sorted((CACHE_DIR / "games_teams").glob("*.json.gz")):
        with gzip.open(path, "rt") as f:
            games = json.load(f)
        for game in games:
            game_id = str(game["id"])
            if game_id not in sides:
                continue
            for team in game["teams"]:
                team_id = sides[game_id].get(team.get("homeAway"))
                if team_id is None:
                    continue
                stats = {}
                for stat in team.get("stats") or []:
                    name = TEAM_STATS.get(stat["category"])
                    if name:
                        put(stats, name, stat.get("stat"))
                team_rows[(game_id, team_id)] = ("cfb", game_id, team_id, SOURCE, Jsonb(stats))

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM player_game_stats WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.execute("DELETE FROM team_game_stats WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.executemany("INSERT INTO player_game_stats (league, game_id, player_id, team_id, source, player_name, stats) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", list(player_rows.values()))
        cur.executemany("INSERT INTO team_game_stats (league, game_id, team_id, source, stats) "
                        "VALUES (%s, %s, %s, %s, %s)", list(team_rows.values()))
    print(f"[cfbd_box] {len(player_rows)} player-games, {len(team_rows)} team-games", flush=True)


def load_rosters(conn):
    lookup = team_ids(conn)
    rows = {}
    for path in sorted((CACHE_DIR / "roster").glob("*.json.gz")):
        season = int(path.name.split(".")[0])
        with gzip.open(path, "rt") as f:
            players = json.load(f)
        for p in players:
            team_id = lookup(season, p.get("team"))
            if team_id is None or not p.get("id"):
                continue
            name = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x)
            hometown = ", ".join(x for x in (p.get("homeCity"), p.get("homeState")) if x)
            year = p.get("year")
            rows[(season, team_id, str(p["id"]))] = (
                "cfb", season, team_id, str(p["id"]), "cfbd", name or "Unknown", p.get("position"),
                p.get("jersey"), p.get("height"), p.get("weight"),
                {1: "FR", 2: "SO", 3: "JR", 4: "SR", 5: "5th"}.get(year) if isinstance(year, int) and year < 10 else None,
                hometown or None, None,
            )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM rosters WHERE league = 'cfb'")
        cur.executemany("INSERT INTO rosters (league, season, team_id, player_id, source, name, position, jersey, height, "
                        "weight, experience, origin, headshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        list(rows.values()))
    print(f"[cfbd_box] {len(rows)} roster rows", flush=True)


def load_polls(conn):
    rows = {}
    for season, weeks in conn.execute(
        "SELECT (params->>'year')::int, payload FROM raw_payloads WHERE source = 'cfbd' AND league = 'cfb' "
        "AND endpoint = 'rankings'"
    ):
        for w in weeks:
            season_type = 3 if w.get("seasonType") == "postseason" else 2
            for poll in w.get("polls") or []:
                if poll["poll"] not in POLLS:
                    continue
                for r in poll.get("ranks") or []:
                    if r.get("teamId") is None:
                        continue
                    key = (season, season_type, w["week"], poll["poll"], str(r["teamId"]))
                    rows[key] = ("cfb", *key, r["rank"], r.get("points"), r.get("firstPlaceVotes"))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM polls WHERE league = 'cfb'")
        cur.executemany("INSERT INTO polls (league, season, season_type, week, poll, team_id, rank, points, "
                        "first_place_votes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", list(rows.values()))
    print(f"[cfbd_box] {len(rows)} poll entries", flush=True)


def load_affiliations(conn):
    rows = {}
    for season, teams in conn.execute(
        "SELECT (params->>'year')::int, payload FROM raw_payloads WHERE source = 'cfbd' AND league = 'cfb' "
        "AND endpoint = 'teams'"
    ):
        for t in teams:
            rows[(season, str(t["id"]))] = ("cfb", season, str(t["id"]), t.get("conference"), t.get("division"),
                                            t.get("classification"))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_affiliations WHERE league = 'cfb'")
        cur.executemany("INSERT INTO team_affiliations (league, season, team_id, conference, division, classification) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", list(rows.values()))
    print(f"[cfbd_box] {len(rows)} team-season affiliations", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load CFB box scores, rosters, polls and conferences from CFBD.")
    parser.add_argument("--start", type=int, default=2005)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--pause", type=float, default=0.5, help="seconds between requests (default 0.5)")
    parser.add_argument("--no-fetch", action="store_true", help="rebuild tables from cached responses only")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if not args.no_fetch:
            fetch(conn, args.start, args.end, args.pause)
        load_box(conn)
        load_rosters(conn)
        load_polls(conn)
        load_affiliations(conn)


if __name__ == "__main__":
    main()
