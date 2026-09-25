"""Run the pipeline stages in order; what the Kubernetes CronJobs execute.

    python pipeline.py refresh   # scores, schedules, lines -> Elo, features, predictions
    python pipeline.py daily     # refresh + play-by-play/EPA, box scores, rosters, polls, CFBD data
    python pipeline.py weekly    # daily + retrain every model

Steps run one at a time and stop at the first failure, so later steps never build on a
half-finished update (the next scheduled run retries). A Postgres advisory lock keeps runs
from overlapping: refresh skips its turn if another run holds it; daily/weekly wait for it.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from backfill import current_season
from database import connect

HERE = Path(__file__).parent
LOCK_ID = 7_310_442  # arbitrary, shared by every pipeline run


def steps(stage):
    season = str(current_season())
    this_season = ["--start", season, "--end", season]
    ingest = [
        ("ESPN scoreboards", ["backfill.py", *this_season]),
        ("clean games/teams", ["etl.py"]),
        ("nflverse schedule, lines, QBs", ["nflverse.py"]),
        ("ESPN lines", ["espn_odds.py", "--league", "nfl", "cfb", *this_season]),
        ("NFL injury reports / depth charts", ["nflverse_availability.py", *this_season]),
        ("CFB injury news (LLM)", ["cfb_news.py"] + (["--teams"] if stage != "refresh" else [])),
    ]
    daily = [
        ("CFBD lines", ["cfbd.py", *this_season]),
        ("CFBD preseason context", ["cfbd_seasons.py", "--start", season]),
        ("NFL play-by-play EPA", ["nflverse_pbp.py", *this_season]),
        ("NFL box scores/rosters", ["nflverse_box.py", *this_season]),
        ("CFB play-by-play", ["espn_pbp.py", *this_season]),
        ("CFB EPA", ["cfb_epa.py"]),
        ("CFB box scores/rosters/polls", ["cfbd_box.py", *this_season]),
        ("CFB transfers/recruits/coaches", ["cfbd_players.py", "--start", str(int(season) - 1)]),
        ("preseason ratings", ["offseason.py"]),
        ("CFB elite-season odds", ["elite.py", "--write"]),  # after offseason.py, which rewrites team_preseason
    ]
    model = [
        ("Elo", ["elo.py"]),
        ("features", ["features.py"]),
    ]
    finish = {
        "refresh": [("predict upcoming", ["predict.py"])],
        "daily": [("predict upcoming", ["predict.py"])],
        "weekly": [("retrain models", ["train.py"]), ("model explainers", ["explain.py"])],
    }[stage]
    return ingest + (daily if stage in ("daily", "weekly") else []) + model + finish


def run(stage):
    with connect() as conn:
        if stage == "refresh":
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
                print("[pipeline] another run is in progress; skipping this refresh", flush=True)
                return 0
        else:
            print("[pipeline] waiting for the pipeline lock...", flush=True)
            conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))

        started = time.time()
        print(f"[pipeline] {stage}: starting", flush=True)
        for name, cmd in steps(stage):
            t = time.time()
            print(f"[pipeline] >>> {name}: {' '.join(cmd)}", flush=True)
            result = subprocess.run([sys.executable, *cmd], cwd=HERE)
            if result.returncode != 0:
                print(f"[pipeline] FAILED at {name!r} (exit {result.returncode}) after {time.time() - t:.0f}s; "
                      "stopping", flush=True)
                return result.returncode
            print(f"[pipeline] <<< {name}: {time.time() - t:.0f}s", flush=True)
        print(f"[pipeline] {stage}: done in {time.time() - started:.0f}s", flush=True)
        return 0  # the lock is released when the connection closes


def main():
    parser = argparse.ArgumentParser(description="Run a pipeline stage.")
    parser.add_argument("stage", choices=["refresh", "daily", "weekly"])
    sys.exit(run(parser.parse_args().stage))


if __name__ == "__main__":
    main()
