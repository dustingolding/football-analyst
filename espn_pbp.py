"""Download ESPN play-by-play (game summaries) and cache the plays locally.

Summaries are ~500 KB, mostly news and boxscore; only the play fields needed for an
expected-points model are kept, gzipped under data/espn_pbp/<league>/<season>/<game_id>.json.gz.
Like the nflverse files, the cache is the raw layer and can be re-parsed at any time.
Resumable: completed games already cached are skipped.

    python espn_pbp.py                         # cfb, 2005 through the current season
    python espn_pbp.py --start 2024 --pause 0.2
"""

import argparse
import gzip
import json
import time
from pathlib import Path

from backfill import current_season
from database import connect
from espn_client import LEAGUES, EspnClient

CACHE_DIR = Path(__file__).parent / "data" / "espn_pbp"
PLAY_FIELDS = ["id", "sequenceNumber", "homeScore", "awayScore", "scoringPlay", "scoringType",
               "isTurnover", "isPenalty", "statYardage", "pointAfterAttempt"]
SPOT_FIELDS = ["down", "distance", "yardLine", "yardsToEndzone"]


def cache_path(league, season, game_id):
    return CACHE_DIR / league / str(season) / f"{game_id}.json.gz"


def trim(summary):
    """Plays in order, with just what the EP model and team/QB aggregates need."""
    plays = []
    for drive_number, drive in enumerate((summary.get("drives") or {}).get("previous") or []):
        for play in drive.get("plays") or []:
            row = {k: play.get(k) for k in PLAY_FIELDS}
            row["drive"] = drive_number
            row["type_id"] = (play.get("type") or {}).get("id")
            row["type"] = (play.get("type") or {}).get("text")
            row["period"] = (play.get("period") or {}).get("number")
            row["clock"] = (play.get("clock") or {}).get("displayValue")
            row["text"] = play.get("text")
            for spot in ("start", "end"):
                s = play.get(spot) or {}
                row[spot] = {k: s.get(k) for k in SPOT_FIELDS}
                row[spot]["team"] = (s.get("team") or {}).get("id")
            plays.append(row)
    competitors = (((summary.get("header") or {}).get("competitions") or [{}])[0]).get("competitors") or []
    teams = {c.get("homeAway"): (c.get("team") or {}).get("id") for c in competitors}
    return {"home": teams.get("home"), "away": teams.get("away"), "plays": plays}


def fetch(conn, league, start, end, pause):
    client = EspnClient(league)
    games = conn.execute(
        "SELECT game_id, season FROM games WHERE league = %s AND completed AND season BETWEEN %s AND %s "
        "ORDER BY season DESC, start_time",
        (league, start, end),
    ).fetchall()
    todo = [(g, s) for g, s in games if not cache_path(league, s, g).exists()]
    print(f"[{league}] {len(todo)} of {len(games)} completed games need play-by-play", flush=True)
    for n, (game_id, season) in enumerate(todo, 1):
        try:
            summary = client.get("summary", params={"event": game_id})
        except Exception as exc:
            print(f"[{league}] {game_id}: FAILED {exc}", flush=True)
            continue
        path = cache_path(league, season, game_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(trim(summary), f)
        if n % 500 == 0:
            print(f"[{league}] {n}/{len(todo)}", flush=True)
        time.sleep(pause)


def main():
    parser = argparse.ArgumentParser(description="Cache ESPN play-by-play for completed games.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=["cfb"])
    parser.add_argument("--start", type=int, default=2005)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--pause", type=float, default=0.1, help="seconds between requests (default 0.1)")
    args = parser.parse_args()

    with connect() as conn:
        for league in args.league:
            fetch(conn, league, args.start, args.end, args.pause)


if __name__ == "__main__":
    main()
