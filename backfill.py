"""Backfill raw ESPN scoreboard and team data into Postgres.

Resumable: weeks from past seasons that are already stored are skipped, so the script
can be stopped and re-run. The current season is always re-fetched because its scores
and schedules are still changing.

    python backfill.py                          # cfb + nfl, 2015 through the current season
    python backfill.py --league cfb --start 2005
    python backfill.py --refresh                # re-fetch everything, even stored weeks
"""

import argparse
import time
from datetime import date

from database import connect, get_raw_payload, init_db, save_raw_payload
from espn_client import LEAGUES, EspnClient

# ESPN falls back to this page size when it rejects the requested limit.
ESPN_DEFAULT_PAGE_SIZE = 25


def current_season():
    today = date.today()
    # Seasons start in August; January/February games belong to the previous year's season.
    return today.year if today.month >= 8 else today.year - 1


def season_weeks(client, conn, year, refresh):
    """Read ESPN's calendar for this season from the regular season week 1 scoreboard.

    Returns ({season_type: [week, ...]}, week_1_payload_if_fetched_now_else_None).
    """
    params = client.games_params(year, week=1, season_type=2)
    payload = None if refresh else get_raw_payload(conn, client.league, "scoreboard", params)
    fetched = None
    if payload is None:
        payload = fetched = client.get("scoreboard", params=params)

    weeks = {}
    for season_type in payload["leagues"][0].get("calendar", []):
        weeks[int(season_type["value"])] = [int(entry["value"]) for entry in season_type.get("entries", [])]
    return weeks, fetched


def check_game_count(params, events):
    count = len(events)
    limit = int(params.get("limit", 0))
    if count == 0:
        return "no games"
    if count == ESPN_DEFAULT_PAGE_SIZE or (limit and count >= limit):
        return f"{count} games - may be truncated (limit={limit})"
    return None


def backfill_league(conn, league, start, end, season_types, refresh, pause):
    def store(league, endpoint, params, status, payload):
        save_raw_payload(conn, league, endpoint, params, status, payload)

    client = EspnClient(league, on_response=store)
    this_season = current_season()

    teams = client.fetch_teams()["sports"][0]["leagues"][0]["teams"]
    print(f"[{league}] teams: {len(teams)}", flush=True)

    for year in range(start, end + 1):
        always_fetch = refresh or year >= this_season
        weeks, week_1_payload = season_weeks(client, conn, year, always_fetch)
        fetched = skipped = 0

        for season_type in season_types:
            for week in weeks.get(season_type, []):
                params = client.games_params(year, week, season_type)
                if week_1_payload is not None and (season_type, week) == (2, 1):
                    # Already fetched (and stored) while reading the calendar.
                    events = week_1_payload.get("events", [])
                else:
                    if not always_fetch and get_raw_payload(conn, league, "scoreboard", params) is not None:
                        skipped += 1
                        continue
                    try:
                        events = client.get("scoreboard", params=params).get("events", [])
                    except Exception as exc:
                        print(f"[{league}] {year} type={season_type} week={week}: FAILED {exc}", flush=True)
                        continue

                fetched += 1
                warning = check_game_count(params, events)
                line = f"[{league}] {year} type={season_type} week={week}: {len(events)} games"
                print(f"{line}  WARNING: {warning}" if warning else line, flush=True)
                time.sleep(pause)

        print(f"[{league}] {year} done: {fetched} weeks fetched, {skipped} already stored", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Backfill raw ESPN data into Postgres.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    parser.add_argument("--start", type=int, default=2015, help="first season (default 2015)")
    parser.add_argument("--end", type=int, default=current_season(), help="last season (default current)")
    parser.add_argument(
        "--season-types", type=int, nargs="+", default=[2, 3],
        help="ESPN season types: 1=preseason, 2=regular, 3=postseason (default 2 3)",
    )
    parser.add_argument("--refresh", action="store_true", help="re-fetch weeks that are already stored")
    parser.add_argument("--pause", type=float, default=0.5, help="seconds between requests (default 0.5)")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            backfill_league(conn, league, args.start, args.end, args.season_types, args.refresh, args.pause)


if __name__ == "__main__":
    main()
