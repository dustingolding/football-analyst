"""Load CFB team-season context from CollegeFootballData into team_seasons.

Everything here describes a team going into (or at the end of) a season, for preseason
priors: SP+ ratings, recruiting class ratings, the 247 talent composite, and returning
production. Raw responses go to raw_payloads (one per endpoint and year); team names are
mapped to ESPN team ids through CFBD's /teams list (CFBD ids are ESPN ids).

Coverage: SP+ and recruiting from the early 2000s, returning production from 2014,
talent from 2015. About 100 calls for a full load; stored years are skipped.

    python cfbd_seasons.py
    python cfbd_seasons.py --refresh --start 2025
"""

import argparse
import os
import time

import requests
from psycopg.types.json import Jsonb

from backfill import current_season
from database import connect, get_raw_payload, init_db, save_raw_payload

BASE_URL = "https://api.collegefootballdata.com"
SOURCE = "cfbd"
# endpoint -> first year CFBD has data for it
ENDPOINTS = {"ratings/sp": 2004, "recruiting/teams": 2001, "talent": 2015, "player/returning": 2014, "teams": 2001}


def fetch(conn, start, end, refresh, pause):
    key = os.getenv("CFBD_API_KEY")
    if not key:
        raise SystemExit("Set CFBD_API_KEY in .env (free key: https://collegefootballdata.com/key).")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {key}"
    calls = 0
    for endpoint, first in ENDPOINTS.items():
        for year in range(max(start, first), end + 1):
            params = {"year": year}
            if not (refresh or year >= current_season()) and get_raw_payload(conn, "cfb", endpoint, params, SOURCE):
                continue
            for attempt in range(5):
                response = session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=(10, 120))
                if response.status_code != 429:
                    break
                time.sleep(10 * (attempt + 1))  # rate limited: back off and retry
            response.raise_for_status()
            time.sleep(pause)
            save_raw_payload(conn, "cfb", endpoint, params, response.status_code, response.json(), source=SOURCE)
            calls += 1
    print(f"[cfbd_seasons] {calls} API calls", flush=True)


def stored(conn, endpoint):
    return conn.execute(
        "SELECT (params->>'year')::int, payload FROM raw_payloads "
        "WHERE source = %s AND league = 'cfb' AND endpoint = %s ORDER BY 1",
        (SOURCE, endpoint),
    ).fetchall()


def team_ids(conn):
    """(year, school) -> ESPN team id, plus a latest-name fallback."""
    by_year, latest = {}, {}
    for year, teams in stored(conn, "teams"):
        for t in teams:
            for name in [t["school"], *(t.get("alternateNames") or [])]:
                by_year[(year, name)] = str(t["id"])
                latest[name] = str(t["id"])
    return lambda year, name: by_year.get((year, name)) or latest.get(name)


def build(conn):
    lookup = team_ids(conn)
    seasons = {}  # (season, team_id) -> stats
    unmatched = set()

    def put(year, name, values):
        team_id = lookup(year, name)
        if team_id is None:
            unmatched.add(name)
            return
        seasons.setdefault((year, team_id), {}).update({k: v for k, v in values.items() if v is not None})

    for year, rows in stored(conn, "ratings/sp"):
        for r in rows:
            put(year, r["team"], {
                "sp_rating": r.get("rating"),
                "sp_offense": (r.get("offense") or {}).get("rating"),
                "sp_defense": (r.get("defense") or {}).get("rating"),
            })
    for year, rows in stored(conn, "recruiting/teams"):
        for r in rows:
            put(year, r["team"], {"recruiting_points": r.get("points"), "recruiting_rank": r.get("rank")})
    for year, rows in stored(conn, "talent"):
        for r in rows:
            put(year, r["team"], {"talent": r.get("talent")})
    for year, rows in stored(conn, "player/returning"):
        for r in rows:
            put(year, r["team"], {
                "returning_ppa_pct": r.get("percentPPA"),
                "returning_passing_ppa_pct": r.get("percentPassingPPA"),
                "returning_usage": r.get("usage"),
            })

    rows = [("cfb", season, team_id, SOURCE, Jsonb(stats)) for (season, team_id), stats in seasons.items()]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_seasons WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.executemany(
            "INSERT INTO team_seasons (league, season, team_id, source, stats) VALUES (%s, %s, %s, %s, %s)", rows
        )
    print(f"[cfbd_seasons] {len(rows)} team-seasons written; {len(unmatched)} team names unmatched "
          f"(e.g. {sorted(unmatched)[:5]})", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load CFBD team-season context (SP+, recruiting, talent, returning).")
    parser.add_argument("--start", type=int, default=2001)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-fetch years already stored")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between requests (default 1.0)")
    parser.add_argument("--no-fetch", action="store_true", help="rebuild team_seasons from stored responses only")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if not args.no_fetch:
            fetch(conn, args.start, args.end, args.refresh, args.pause)
        build(conn)


if __name__ == "__main__":
    main()
