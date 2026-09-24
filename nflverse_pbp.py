"""Per-team NFL efficiency (EPA, success rate) from nflverse play-by-play.

Play-by-play files (~20 MB per season) are cached in data/nflverse/ instead of
raw_payloads; they are the raw layer here and can be re-parsed at any time. Only
per-team, per-game aggregates go into team_game_stats.

Plays counted: runs and passes with an EPA value, while the game is competitive
(win probability 5-95%), so garbage time doesn't distort a team's numbers.

    python nflverse_pbp.py                 # 2005 through the current season
    python nflverse_pbp.py --start 2024    # current season files are always re-downloaded
"""

import argparse
from pathlib import Path

import pandas as pd
import requests
from psycopg.types.json import Jsonb

import nflverse
from backfill import current_season
from database import connect, init_db

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
CACHE_DIR = Path(__file__).parent / "data" / "nflverse"
SOURCE = "nflverse_pbp"
COLUMNS = ["game_id", "posteam", "defteam", "pass", "rush", "epa", "success", "wp", "cpoe"]
WP_RANGE = (0.05, 0.95)
# Play-by-play uses current abbreviations for relocated teams; games.csv keeps the old ones.
CURRENT_ABBR = {"STL": "LA", "SD": "LAC", "OAK": "LV"}


def pbp_file(season, refresh):
    path = CACHE_DIR / f"play_by_play_{season}.csv.gz"
    if refresh or not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        response = requests.get(PBP_URL.format(season=season), timeout=(10, 300))
        response.raise_for_status()
        path.write_bytes(response.content)
        print(f"[nflverse_pbp] downloaded {season} ({len(response.content) / 1e6:.0f} MB)", flush=True)
    return path


def team_stats(plays):
    """One row per (nflverse game_id, team) with offensive and defensive efficiency."""
    plays = plays[((plays["pass"] == 1) | (plays["rush"] == 1)) & plays["epa"].notna()]
    plays = plays[plays["wp"].between(*WP_RANGE)]

    def side(group_col, prefix):
        g = plays.groupby(["game_id", group_col])
        out = pd.DataFrame({
            f"{prefix}_plays": g.size(),
            f"{prefix}_epa": g["epa"].mean(),
            f"{prefix}_success": g["success"].mean(),
            f"{prefix}_pass_epa": plays[plays["pass"] == 1].groupby(["game_id", group_col])["epa"].mean(),
            f"{prefix}_rush_epa": plays[plays["rush"] == 1].groupby(["game_id", group_col])["epa"].mean(),
            f"{prefix}_pass_rate": g["pass"].mean(),
        })
        return out.rename_axis(["game_id", "team"])

    offense, defense = side("posteam", "off"), side("defteam", "def")
    offense["off_cpoe"] = plays.groupby(["game_id", "posteam"])["cpoe"].mean().rename_axis(["game_id", "team"])
    return offense.join(defense, how="outer").reset_index()


def load_season(conn, season, refresh, game_map):
    plays = pd.read_csv(pbp_file(season, refresh), usecols=COLUMNS, low_memory=False)
    stats = team_stats(plays)

    rows = []
    for record in stats.to_dict("records"):
        match = game_map.get(record.pop("game_id"))
        team = record.pop("team")
        team = CURRENT_ABBR.get(team, team)
        if match is None or team not in match:
            continue
        espn_game_id, team_id = match["espn"], match[team]
        values = {k: (None if pd.isna(v) else round(float(v), 5)) for k, v in record.items()}
        rows.append(("nfl", espn_game_id, team_id, SOURCE, Jsonb(values)))

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO team_game_stats (league, game_id, team_id, source, stats) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (league, game_id, team_id, source) DO UPDATE SET stats = EXCLUDED.stats, updated_at = now()",
            rows,
        )
    print(f"[nflverse_pbp] {season}: {len(plays)} plays -> {len(rows)} team-games "
          f"({stats['game_id'].nunique()} games in file)", flush=True)


def game_map(conn):
    """nflverse game_id -> {"espn": ESPN game_id, <nflverse team abbr>: ESPN team_id}."""
    teams = {
        game_id: (home, away)
        for game_id, home, away in conn.execute("SELECT game_id, home_team_id, away_team_id FROM games WHERE league = 'nfl'")
    }
    out = {}
    for espn_id, row in nflverse.matched_rows(conn, nflverse.stored_rows(conn)):
        home, away = teams[espn_id]
        home_abbr = CURRENT_ABBR.get(row["home_team"], row["home_team"])
        away_abbr = CURRENT_ABBR.get(row["away_team"], row["away_team"])
        out[row["game_id"]] = {"espn": espn_id, home_abbr: home, away_abbr: away}
    return out


def main():
    parser = argparse.ArgumentParser(description="Load nflverse play-by-play efficiency into team_game_stats.")
    parser.add_argument("--start", type=int, default=2005)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-download cached files")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        mapping = game_map(conn)
        for season in range(args.start, args.end + 1):
            load_season(conn, season, args.refresh or season >= current_season(), mapping)


if __name__ == "__main__":
    main()
