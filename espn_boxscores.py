"""Backfill ESPN player box scores (game_boxscores) for completed games that don't have a final one yet.
live.py captures box scores while games are on; this catches games it missed (downtime, first install).

    python espn_boxscores.py                  # this season, both leagues, up to --limit games per league
    python espn_boxscores.py --season 2025 --limit 2000
"""
import argparse
import time

import requests

from database import connect, init_db
from espn_client import EspnClient
from live import save_boxscore


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", choices=["nfl", "cfb"], action="append")
    ap.add_argument("--season", type=int, help="default: each league's latest season")
    ap.add_argument("--limit", type=int, default=150, help="games per league per run")
    args = ap.parse_args()
    with connect() as conn:
        init_db(conn)
        for league in args.league or ["nfl", "cfb"]:
            season = args.season or conn.execute("SELECT max(season) FROM games WHERE league = %s AND completed",
                                                 (league,)).fetchone()[0]
            # college: games with at least one FBS team (a preseason rating), newest first
            fbs = "" if league == "nfl" else """AND EXISTS (SELECT 1 FROM team_preseason tp WHERE tp.league = g.league
                AND tp.season = g.season AND tp.team_id IN (g.home_team_id, g.away_team_id))"""
            ids = [r[0] for r in conn.execute(f"""
                SELECT g.game_id FROM games g
                WHERE g.league = %s AND g.season = %s AND g.completed AND g.start_time < now() - interval '30 minutes' {fbs}
                  AND NOT EXISTS (SELECT 1 FROM game_boxscores b WHERE b.league = g.league AND b.game_id = g.game_id AND b.final)
                ORDER BY g.start_time DESC LIMIT %s""", (league, season, args.limit)).fetchall()]
            client = EspnClient(league, max_attempts=3, timeout=(5, 30))
            saved = failed = 0
            for gid in ids:
                try:
                    saved += save_boxscore(conn, league, gid, client.get("summary", params={"event": gid}), True)
                except requests.RequestException as e:
                    failed += 1
                    print(f"[box] {league} {gid}: {e}", flush=True)
                time.sleep(0.2)
            print(f"[box] {league} {season}: {len(ids)} missing, saved {saved}, failed {failed}", flush=True)


if __name__ == "__main__":
    main()
