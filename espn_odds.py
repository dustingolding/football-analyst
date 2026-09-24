"""Backfill betting lines from ESPN's core API into raw_payloads and the odds table.

One request per game, so this is slow (~12k CFB games since 2012); it is resumable and
skips games already stored, except upcoming and current-season games whose lines move.
ESPN has lines from about 2012 on; earlier games return nothing and are not requested.

    python espn_odds.py                      # cfb, 2012 through the current season
    python espn_odds.py --league nfl cfb --start 2020
    python espn_odds.py --no-fetch           # rebuild odds from stored responses only
"""

import argparse
import time

import requests

from backfill import current_season
from database import connect, init_db, save_raw_payload
from espn_client import LEAGUES, EspnClient

SOURCE = "espn"
ENDPOINT = "odds"
FIRST_SEASON = 2012

ODDS_COLUMNS = [
    "league", "game_id", "source", "provider", "home_spread", "total",
    "home_moneyline", "away_moneyline", "home_spread_odds", "away_spread_odds", "over_odds", "under_odds",
    "opening_home_spread", "opening_total",
]


def games_to_fetch(conn, league, start, end, refresh):
    return [game_id for (game_id,) in conn.execute(
        """
        SELECT g.game_id FROM games g
        WHERE g.league = %(league)s AND g.season BETWEEN %(start)s AND %(end)s
          AND (%(refresh)s OR g.season >= %(current)s OR NOT EXISTS (
              SELECT 1 FROM raw_payloads r
              WHERE r.source = %(source)s AND r.league = g.league AND r.endpoint = %(endpoint)s
                AND r.params = jsonb_build_object('event', g.game_id)))
        ORDER BY g.start_time
        """,
        {"league": league, "start": start, "end": end, "refresh": refresh, "current": current_season(),
         "source": SOURCE, "endpoint": ENDPOINT},
    )]


def fetch(conn, league, start, end, refresh, pause):
    client = EspnClient(league)
    game_ids = games_to_fetch(conn, league, start, end, refresh)
    print(f"[{league}] fetching odds for {len(game_ids)} games", flush=True)
    for n, game_id in enumerate(game_ids, 1):
        try:
            payload, status = client.fetch_odds(game_id), 200
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                print(f"[{league}] {game_id}: FAILED {exc}", flush=True)
                continue
            payload, status = {"items": []}, 404  # stored so the game isn't requested again
        except Exception as exc:
            print(f"[{league}] {game_id}: FAILED {exc}", flush=True)
            continue
        save_raw_payload(conn, league, ENDPOINT, {"event": game_id}, status, payload, source=SOURCE)
        if n % 500 == 0:
            print(f"[{league}] {n}/{len(game_ids)}", flush=True)
        time.sleep(pause)


def american(value):
    try:
        return int(float(str(value).replace("+", "")))
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        return float(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None


def parse_item(league, game_id, item):
    provider = (item.get("provider") or {}).get("name") or "unknown"
    if "live" in provider.lower():  # in-game lines, not pre-game
        return None
    if item.get("spread") is None and item.get("overUnder") is None:
        return None
    home, away = item.get("homeTeamOdds") or {}, item.get("awayTeamOdds") or {}
    home_open = home.get("open") or {}
    return {
        "league": league,
        "game_id": game_id,
        "source": SOURCE,
        "provider": provider,
        # ESPN's spread is already from the home side (negative = home favored).
        "home_spread": number(item.get("spread")),
        "total": number(item.get("overUnder")),
        "home_moneyline": american(home.get("moneyLine")),
        "away_moneyline": american(away.get("moneyLine")),
        "home_spread_odds": american(home.get("spreadOdds")),
        "away_spread_odds": american(away.get("spreadOdds")),
        "over_odds": american(item.get("overOdds")),
        "under_odds": american(item.get("underOdds")),
        "opening_home_spread": number((home_open.get("pointSpread") or {}).get("american")),
        "opening_total": number(((item.get("open") or {}).get("total") or {}).get("american")),
    }


def load_odds(conn, league):
    odds = {}
    cur = conn.execute(
        "SELECT params->>'event', payload FROM raw_payloads "
        "WHERE source = %s AND league = %s AND endpoint = %s ORDER BY fetched_at, id",
        (SOURCE, league, ENDPOINT),
    )
    for game_id, payload in cur:
        for item in payload.get("items", []):
            line = parse_item(league, game_id, item)
            if line:
                odds[(game_id, line["provider"])] = line

    placeholders = ", ".join(["%s"] * len(ODDS_COLUMNS))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM odds WHERE league = %s AND source = %s", (league, SOURCE))
        cur.executemany(
            f"INSERT INTO odds ({', '.join(ODDS_COLUMNS)}) VALUES ({placeholders})",
            [[o[c] for c in ODDS_COLUMNS] for o in odds.values()],
        )
    print(f"[{league}] {len(odds)} espn lines for {len({g for g, _ in odds})} games loaded", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Backfill ESPN betting lines into odds.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=["cfb"])
    parser.add_argument("--start", type=int, default=FIRST_SEASON)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-fetch games that are already stored")
    parser.add_argument("--no-fetch", action="store_true", help="only rebuild odds from stored responses")
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between requests (default 0.2)")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            if not args.no_fetch:
                fetch(conn, league, args.start, args.end, args.refresh, args.pause)
            load_odds(conn, league)


if __name__ == "__main__":
    main()
