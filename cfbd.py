"""Load college football betting lines from CollegeFootballData.com into the odds table.

Needs a free API key (https://collegefootballdata.com/key) as CFBD_API_KEY (k8s Secret pipeline-env; on the host run via ./kenv).
CFBD game ids are ESPN event ids, so lines join straight onto games.game_id. Each
season/season type is one request (about 44 calls for 2005 onward; the free tier allows
1,000 per month), and past seasons already stored are skipped unless --refresh is given.

    python cfbd.py                     # 2005 through the current season
    python cfbd.py --start 2024 --refresh
    python cfbd.py --no-fetch          # rebuild odds from stored responses only
"""

import argparse
import os
import time

import requests

from backfill import current_season
from database import connect, get_raw_payload, init_db, save_raw_payload

BASE_URL = "https://api.collegefootballdata.com"
SOURCE = "cfbd"
SEASON_TYPES = ["regular", "postseason"]

ODDS_COLUMNS = [
    "league", "game_id", "source", "provider", "home_spread", "total",
    "home_moneyline", "away_moneyline", "opening_home_spread", "opening_total",
]


def fetch_lines(conn, start, end, refresh, pause):
    key = os.getenv("CFBD_API_KEY")
    if not key:
        raise SystemExit("Set CFBD_API_KEY (Secret pipeline-env; on the host: ./kenv dev|prod ...). Free key: https://collegefootballdata.com/key")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {key}"

    for year in range(start, end + 1):
        for season_type in SEASON_TYPES:
            params = {"year": year, "seasonType": season_type}
            if not (refresh or year >= current_season()) and get_raw_payload(conn, "cfb", "lines", params, SOURCE):
                continue
            response = session.get(f"{BASE_URL}/lines", params=params, timeout=(10, 120))
            response.raise_for_status()
            games = response.json()
            save_raw_payload(conn, "cfb", "lines", params, response.status_code, games, source=SOURCE)
            print(f"[cfbd] {year} {season_type}: {len(games)} games", flush=True)
            time.sleep(pause)


def number(value, cast=float):
    return cast(value) if value is not None else None


def parse_lines(game):
    for line in game.get("lines") or []:
        if line.get("spread") is None and line.get("overUnder") is None:
            continue
        yield {
            "league": "cfb",
            "game_id": str(game["id"]),
            "source": SOURCE,
            "provider": line.get("provider") or "unknown",
            # CFBD spreads are already from the home side (negative = home favored).
            "home_spread": number(line.get("spread")),
            "total": number(line.get("overUnder")),
            "home_moneyline": number(line.get("homeMoneyline"), int),
            "away_moneyline": number(line.get("awayMoneyline"), int),
            "opening_home_spread": number(line.get("spreadOpen")),
            "opening_total": number(line.get("overUnderOpen")),
        }


def load_odds(conn):
    known = {game_id for (game_id,) in conn.execute("SELECT game_id FROM games WHERE league = 'cfb'")}
    odds, unmatched = {}, 0
    cur = conn.execute(
        "SELECT payload FROM raw_payloads WHERE source = %s AND league = 'cfb' AND endpoint = 'lines' "
        "ORDER BY fetched_at, id",
        (SOURCE,),
    )
    for (games,) in cur:
        for game in games:
            for line in parse_lines(game):
                if line["game_id"] not in known:
                    unmatched += 1
                    continue
                odds[(line["game_id"], line["provider"])] = line

    placeholders = ", ".join(["%s"] * len(ODDS_COLUMNS))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM odds WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.executemany(
            f"INSERT INTO odds ({', '.join(ODDS_COLUMNS)}) VALUES ({placeholders})",
            [[o[c] for c in ODDS_COLUMNS] for o in odds.values()],
        )
    games_with_odds = len({game_id for game_id, _ in odds})
    print(f"[cfbd] {len(odds)} lines for {games_with_odds} games loaded, {unmatched} lines not in games table",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load CollegeFootballData betting lines into odds.")
    parser.add_argument("--start", type=int, default=2005)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-fetch seasons that are already stored")
    parser.add_argument("--no-fetch", action="store_true", help="only rebuild odds from stored responses")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between requests (default 1.0)")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if not args.no_fetch:
            fetch_lines(conn, args.start, args.end, args.refresh, args.pause)
        load_odds(conn)


if __name__ == "__main__":
    main()
