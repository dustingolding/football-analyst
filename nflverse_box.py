"""NFL box scores, rosters and divisions from nflverse.

    stats_player_week_<season>.csv   per-player game stats (incl. EPA)
    stats_team_week_<season>.csv     per-team game stats
    roster_<season>.csv              season rosters
    teams_colors_logos.csv           conference and division

Files are cached in data/nflverse/ (the current season is always re-downloaded) and mapped
onto ESPN game and team ids through the nflverse schedule, which nflverse.py has already
matched to ESPN games. Stat names match cfbd_box.py so the web pages treat both leagues alike.

    python nflverse_box.py
    python nflverse_box.py --start 2025
"""

import argparse
from pathlib import Path

import pandas as pd
import requests
from psycopg.types.json import Jsonb

import nflverse
from backfill import current_season
from database import connect, init_db

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
CACHE_DIR = Path(__file__).parent / "data" / "nflverse"
SOURCE = "box"
ROSTER_STATUSES = {"ACT", "RES", "INA", "DEV"}  # drop CUT / RET / TRD rows

# nflverse column -> our stat name (shared with cfbd_box.py)
PLAYER_STATS = {
    "completions": "pass_cmp", "attempts": "pass_att", "passing_yards": "pass_yds", "passing_tds": "pass_td",
    "passing_interceptions": "pass_int", "sacks_suffered": "sacks_taken", "passing_epa": "pass_epa",
    "carries": "rush_att", "rushing_yards": "rush_yds", "rushing_tds": "rush_td", "rushing_epa": "rush_epa",
    "receptions": "rec", "targets": "targets", "receiving_yards": "rec_yds", "receiving_tds": "rec_td",
    "receiving_epa": "rec_epa", "def_tackles_solo": "solo", "def_tackles_for_loss": "tfl", "def_sacks": "sacks",
    "def_qb_hits": "qb_hits", "def_interceptions": "def_int", "def_interception_yards": "int_yds",
    "def_pass_defended": "pass_def", "def_tds": "def_td", "def_fumbles_forced": "forced_fumbles",
    "fg_made": "fgm", "fg_att": "fga", "fg_long": "fg_long", "pat_made": "xpm", "pat_att": "xpa",
    "kickoff_returns": "kr", "kickoff_return_yards": "kr_yds", "punt_returns": "pr", "punt_return_yards": "pr_yds",
}


def cached_csv(name, refresh):
    path = CACHE_DIR / f"{name}.csv"
    if refresh or not path.exists():
        tag = name.rsplit("_", 1)[0] if name[-4:].isdigit() else "teams"
        tag = {"stats_player_week": "stats_player", "stats_team_week": "stats_team", "roster": "rosters"}.get(tag, tag)
        response = requests.get(f"{RELEASES}/{tag}/{name}.csv", timeout=(10, 300))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    return pd.read_csv(path, low_memory=False)


def mappings(conn):
    """nflverse game_id -> ESPN game_id, and nflverse team abbreviation -> ESPN team_id."""
    teams = {gid: (h, a) for gid, h, a in conn.execute(
        "SELECT game_id, home_team_id, away_team_id FROM games WHERE league = 'nfl'")}
    games, abbrs = {}, {}
    for espn_id, row in nflverse.matched_rows(conn, nflverse.stored_rows(conn)):
        games[row["game_id"]] = espn_id
        home, away = teams[espn_id]
        abbrs[row["home_team"]], abbrs[row["away_team"]] = home, away
    # Weekly stats use current abbreviations for relocated teams even in old seasons.
    for old, new in {"STL": "LA", "SD": "LAC", "OAK": "LV"}.items():
        if old in abbrs:
            abbrs.setdefault(new, abbrs[old])
    return games, abbrs


def clean(values):
    out = {}
    for k, v in values.items():
        if v is not None and not pd.isna(v):
            out[k] = round(float(v), 4)
    return out


def load_season(conn, season, refresh, games, abbrs):
    players = cached_csv(f"stats_player_week_{season}", refresh)
    teams = cached_csv(f"stats_team_week_{season}", refresh)
    roster = cached_csv(f"roster_{season}", refresh)

    player_rows = []
    if players is not None:
        for r in players.to_dict("records"):
            game_id, team_id = games.get(r.get("game_id")), abbrs.get(r.get("team"))
            if game_id is None or team_id is None or not isinstance(r.get("player_id"), str):
                continue
            stats = clean({ours: r.get(theirs) for theirs, ours in PLAYER_STATS.items()})
            tackles = sum(r.get(c) or 0 for c in ("def_tackles_solo", "def_tackles_with_assist", "def_tackle_assists")
                          if not pd.isna(r.get(c)))
            if tackles:
                stats["tackles"] = float(tackles)
            lost = sum(r.get(c) or 0 for c in ("sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost")
                       if not pd.isna(r.get(c)))
            if lost:
                stats["fumbles_lost"] = float(lost)
            stats = {k: v for k, v in stats.items() if v != 0 or k in ("pass_att", "rush_att", "rec", "targets")}
            if stats:
                name = r.get("player_display_name") or r.get("player_name") or "Unknown"
                player_rows.append(("nfl", game_id, r["player_id"], team_id, SOURCE, name, Jsonb(stats)))

    team_rows = []
    if teams is not None:
        for r in teams.to_dict("records"):
            game_id, team_id = games.get(r.get("game_id")), abbrs.get(r.get("team"))
            if game_id is None or team_id is None:
                continue
            g = lambda c: 0.0 if pd.isna(r.get(c)) else float(r.get(c) or 0)  # noqa: E731
            pass_net = g("passing_yards") - g("sack_yards_lost")
            team_rows.append(("nfl", game_id, team_id, SOURCE, Jsonb(clean({
                "pass_cmp": g("completions"), "pass_att": g("attempts"), "pass_yds": pass_net,
                "pass_td": g("passing_tds"), "rush_att": g("carries"), "rush_yds": g("rushing_yards"),
                "rush_td": g("rushing_tds"), "total_yds": pass_net + g("rushing_yards"),
                "first_downs": g("passing_first_downs") + g("rushing_first_downs"),
                "ints_thrown": g("passing_interceptions"),
                "fumbles_lost": g("sack_fumbles_lost") + g("rushing_fumbles_lost") + g("receiving_fumbles_lost"),
                "turnovers": g("passing_interceptions") + g("sack_fumbles_lost") + g("rushing_fumbles_lost")
                             + g("receiving_fumbles_lost"),
                "sacks_taken": g("sacks_suffered"), "sacks": g("def_sacks"), "def_int": g("def_interceptions"),
                "tfl": g("def_tackles_for_loss"), "penalties": g("penalties"), "penalty_yds": g("penalty_yards"),
                "pass_epa": g("passing_epa"), "rush_epa": g("rushing_epa"),
            }))))

    roster_rows = {}
    if roster is not None:
        for r in roster.to_dict("records"):
            team_id, player_id = abbrs.get(r.get("team")), r.get("gsis_id")
            status = r.get("status")
            if team_id is None or not isinstance(player_id, str):
                continue
            if isinstance(status, str) and status not in ROSTER_STATUSES:
                continue
            num = lambda c: None if pd.isna(r.get(c)) else int(r.get(c))  # noqa: E731
            college = r.get("college")
            roster_rows[(team_id, player_id)] = (
                "nfl", season, team_id, player_id, "nflverse", r.get("full_name") or "Unknown", r.get("position"),
                num("jersey_number"), num("height"), num("weight"),
                None if pd.isna(r.get("years_exp")) else ("R" if int(r["years_exp"]) == 0 else str(int(r["years_exp"]))),
                college if isinstance(college, str) else None,
                r.get("headshot_url") if isinstance(r.get("headshot_url"), str) else None,
            )

    with conn.transaction(), conn.cursor() as cur:
        season_games = [gid for (gid,) in conn.execute(
            "SELECT game_id FROM games WHERE league = 'nfl' AND season = %s", (season,))]
        cur.execute("DELETE FROM player_game_stats WHERE league = 'nfl' AND source = %s AND game_id = ANY(%s)",
                    (SOURCE, season_games))
        cur.execute("DELETE FROM team_game_stats WHERE league = 'nfl' AND source = %s AND game_id = ANY(%s)",
                    (SOURCE, season_games))
        cur.execute("DELETE FROM rosters WHERE league = 'nfl' AND season = %s", (season,))
        cur.executemany("INSERT INTO player_game_stats (league, game_id, player_id, team_id, source, player_name, stats) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", player_rows)
        cur.executemany("INSERT INTO team_game_stats (league, game_id, team_id, source, stats) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", team_rows)
        cur.executemany("INSERT INTO rosters (league, season, team_id, player_id, source, name, position, jersey, "
                        "height, weight, experience, origin, headshot) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", list(roster_rows.values()))
    print(f"[nflverse_box] {season}: {len(player_rows)} player-games, {len(team_rows)} team-games, "
          f"{len(roster_rows)} roster rows", flush=True)


def load_affiliations(conn, abbrs, start, end):
    teams = cached_csv("teams_colors_logos", refresh=False)
    divisions = {}
    for r in teams.to_dict("records"):
        team_id = abbrs.get(r["team_abbr"])
        if team_id:
            divisions[team_id] = (r["team_conf"], r["team_division"])
    rows = [("nfl", season, team_id, conf, div, None)
            for season in range(start, end + 1) for team_id, (conf, div) in divisions.items()]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_affiliations WHERE league = 'nfl'")
        cur.executemany("INSERT INTO team_affiliations (league, season, team_id, conference, division, classification) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", rows)
    print(f"[nflverse_box] {len(divisions)} teams' divisions for {start}-{end}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load NFL box scores, rosters and divisions from nflverse.")
    parser.add_argument("--start", type=int, default=2005)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--refresh", action="store_true", help="re-download cached files")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        games, abbrs = mappings(conn)
        for season in range(args.start, args.end + 1):
            load_season(conn, season, args.refresh or season >= current_season(), games, abbrs)
        load_affiliations(conn, abbrs, 2005, args.end)


if __name__ == "__main__":
    main()
