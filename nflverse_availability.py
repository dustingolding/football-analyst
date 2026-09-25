"""NFL player availability per game: who's hurt, and how much of the lineup that is.

From nflverse weekly injury reports (2009+) and depth charts (weekly through 2024, daily
snapshots from 2025), for each team-game:
    starters_out        depth-chart starters listed Out / Doubtful on the final report
      ol_out, skill_out, front_out, db_out   ... by unit (QB availability is already handled by
                                             the listed starter's QB rating in features.py)
    starters_questionable
    missing_off_prod    season-to-date share of the team's rushing + receiving yards held by players
                        listed Out / Doubtful (games before this one only)
    missing_def_prod    same for tackles + havoc plays (TFL, sacks, INT, passes defended)
All of it is published before kickoff. Stored in team_game_stats (source 'availability').

    python nflverse_availability.py
    python nflverse_availability.py --start 2026
"""

import argparse
import time
from collections import defaultdict

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

import requests

import nflverse
from backfill import current_season
from database import connect, init_db
from nflverse_box import CACHE_DIR, RELEASES

SOURCE = "availability"
FIRST_SEASON = 2009
OUT = {"Out", "Doubtful"}
UNITS = {
    "ol": {"LT", "LG", "C", "RG", "RT", "T", "G", "OT", "OG", "OL"},
    "skill": {"RB", "WR", "TE", "FB", "HB"},
    "front": {"DE", "DT", "NT", "LDE", "RDE", "LDT", "RDT", "DL", "LB", "ILB", "OLB", "MLB", "LILB", "RILB", "SLB",
              "WLB", "EDGE"},
    "db": {"CB", "LCB", "RCB", "NB", "S", "FS", "SS", "SAF", "DB"},
}
UNIT_SIDE = {"ol": "off", "skill": "off", "front": "def", "db": "def"}


MAX_AGE_SECONDS = 2 * 3600  # current-season files (depth charts ~40 MB) re-downloaded at most every 2 hours


def csv(name, season, refresh):
    path = CACHE_DIR / f"{name}_{season}.csv"
    stale = not path.exists() or (refresh and time.time() - path.stat().st_mtime > MAX_AGE_SECONDS)
    if stale:
        response = requests.get(f"{RELEASES}/{name}/{name}_{season}.csv", timeout=(10, 300))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    return pd.read_csv(path, low_memory=False)


def unit_of(position):
    position = (position or "").upper()
    return next((u for u, positions in UNITS.items() if position in positions), None)


def schedule(conn):
    """(season, week, nflverse team abbr) -> (ESPN game_id, ESPN team_id, kickoff), from the matched schedule."""
    teams = {gid: (h, a, t) for gid, h, a, t in conn.execute(
        "SELECT game_id, home_team_id, away_team_id, start_time FROM games WHERE league = 'nfl'")}
    out = {}
    for espn_id, row in nflverse.matched_rows(conn, nflverse.stored_rows(conn)):
        home, away, kickoff = teams[espn_id]
        key = (int(row["season"]), int(row["week"]))
        out[(*key, row["home_team"])] = (espn_id, home, kickoff)
        out[(*key, row["away_team"])] = (espn_id, away, kickoff)
    return out


def starters(depth, games_by_team):
    """{(game_id, team_id): {player_id: unit}} for depth-chart starters before each game."""
    out = defaultdict(dict)
    if depth is None:
        return out
    if "depth_team" in depth.columns:  # weekly format (through 2024)
        d = depth[(depth["depth_team"] == 1) & depth["formation"].isin(["Offense", "Defense"])]
        for r in d.itertuples(index=False):
            game = games_by_team.get((int(r.season), int(r.week), r.club_code)) if not pd.isna(r.week) else None
            unit = unit_of(r.depth_position) or unit_of(r.position)
            if game and unit and isinstance(r.gsis_id, str):
                out[(game[0], game[1])][r.gsis_id] = unit
        return out
    # Daily snapshots (2025+): the latest snapshot before each game's kickoff.
    d = depth[(depth["pos_rank"] == 1) & ~depth["pos_grp"].str.contains("Special", na=False)].copy()
    d["dt"] = pd.to_datetime(d["dt"], utc=True)
    snapshots = {team: g for team, g in d.groupby("team")}
    for (season, week, team), (game_id, team_id, kickoff) in games_by_team.items():
        g = snapshots.get(team)
        if g is None or kickoff is None:
            continue
        before = g[g["dt"] < pd.Timestamp(kickoff)]
        if before.empty:
            continue
        latest = before[before["dt"] == before["dt"].max()]
        for r in latest.itertuples(index=False):
            unit = unit_of(r.pos_abb)
            if unit and isinstance(r.gsis_id, str):
                out[(game_id, team_id)][r.gsis_id] = unit
    return out


def production_to_date(conn, season):
    """{(game_id, team_id): {player_id: (off share, def share)}}: each player's season-to-date share
    of his team's rushing+receiving yards and tackles+havoc, from games before this one."""
    rows = conn.execute("""
        SELECT g.game_id, g.start_time, s.team_id, s.player_id, s.stats FROM player_game_stats s
        JOIN games g USING (league, game_id)
        WHERE s.league = 'nfl' AND s.source = 'box' AND g.season = %s ORDER BY g.start_time""", (season,)).fetchall()
    games = defaultdict(list)
    for game_id, start, team, player, stats in rows:
        games[(start, game_id, team)].append((player, stats))
    running = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # team -> player -> [off, def]
    out = {}
    for (start, game_id, team), players in sorted(games.items(), key=lambda kv: kv[0][0]):
        totals = running[team]
        off_total = sum(v[0] for v in totals.values()) or 1.0
        def_total = sum(v[1] for v in totals.values()) or 1.0
        out[(game_id, team)] = {p: (v[0] / off_total, v[1] / def_total) for p, v in totals.items()}
        for player, stats in players:
            totals[player][0] += (stats.get("rush_yds") or 0) + (stats.get("rec_yds") or 0)
            totals[player][1] += ((stats.get("tackles") or 0) + (stats.get("tfl") or 0) + (stats.get("sacks") or 0)
                                  + (stats.get("def_int") or 0) + (stats.get("pass_def") or 0))
    return out


def load_season(conn, season, refresh, games):
    injuries = csv("injuries", season, refresh)
    depth = csv("depth_charts", season, refresh)
    if injuries is None:
        print(f"[availability] {season}: no injury report file", flush=True)
        return
    games_by_team = {k: v for k, v in games.items() if k[0] == season}
    lineup = starters(depth, games_by_team)
    shares = production_to_date(conn, season)
    status = defaultdict(dict)  # (game_id, team_id) -> player -> report status
    listed = {}                  # per-player rows for player_status (game pages)
    for r in injuries.itertuples(index=False):
        if pd.isna(r.week) or not isinstance(r.gsis_id, str) or not isinstance(r.report_status, str):
            continue
        game = games_by_team.get((int(r.season), int(r.week), r.team))
        if game:
            status[(game[0], game[1])][r.gsis_id] = r.report_status
            if r.report_status in ("Out", "Doubtful", "Questionable"):
                injury = r.report_primary_injury if isinstance(r.report_primary_injury, str) else None
                listed[(game[0], game[1], r.full_name)] = (
                    "nfl", "nfl_injury_report", game[0], game[1], r.full_name, r.gsis_id, r.position,
                    r.report_status.lower(), None, game[2], injury, None)

    rows = []
    for (season_, week, team), (game_id, team_id, _) in games_by_team.items():
        key = (game_id, team_id)
        report, starting, share = status.get(key, {}), lineup.get(key, {}), shares.get(key, {})
        f = {"starters_listed": len(starting)}
        for unit in UNITS:
            f[f"{unit}_out"] = float(sum(1 for p, u in starting.items() if u == unit and report.get(p) in OUT))
        f["starters_out_off"] = f["ol_out"] + f["skill_out"]
        f["starters_out_def"] = f["front_out"] + f["db_out"]
        f["starters_questionable"] = float(sum(1 for p in starting if report.get(p) == "Questionable"))
        f["missing_off_prod"] = round(sum(share.get(p, (0, 0))[0] for p, s in report.items() if s in OUT), 4)
        f["missing_def_prod"] = round(sum(share.get(p, (0, 0))[1] for p, s in report.items() if s in OUT), 4)
        f["players_out"] = float(sum(1 for s in report.values() if s in OUT))
        rows.append(("nfl", game_id, team_id, SOURCE, Jsonb(f)))
    with conn.transaction(), conn.cursor() as cur:
        ids = list({r[1] for r in rows})
        cur.execute("DELETE FROM team_game_stats WHERE league = 'nfl' AND source = %s AND game_id = ANY(%s)",
                    (SOURCE, ids))
        cur.executemany("INSERT INTO team_game_stats (league, game_id, team_id, source, stats) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", rows)
        # Per-player report lines (the injury goes in the headline column) for game pages.
        cur.execute("DELETE FROM player_status WHERE league = 'nfl' AND source = 'nfl_injury_report' "
                    "AND source_id = ANY(%s)", (ids,))
        cur.executemany("INSERT INTO player_status (league, source, source_id, team_id, player_name, player_id, position, "
                        "status, games, published, headline, url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING", list(listed.values()))
    with_lineup = sum(1 for r in rows if r[4].obj["starters_listed"] > 0)
    print(f"[availability] {season}: {len(rows)} team-games, {with_lineup} with a depth chart, "
          f"{sum(len(v) for v in status.values())} report entries", flush=True)


def main():
    parser = argparse.ArgumentParser(description="NFL availability (injury reports x depth charts) per team-game.")
    parser.add_argument("--start", type=int, default=FIRST_SEASON)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-download cached files")
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        games = schedule(conn)
        for season in range(args.start, args.end + 1):
            load_season(conn, season, args.refresh or season >= current_season(), games)


if __name__ == "__main__":
    main()
