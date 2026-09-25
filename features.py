"""Build one feature row per game from information available before kickoff.

Team form features come from each team's most recent game that finished *before* this
kickoff (a merge_asof on start time), so no row can see its own result or anything
later. Scheduled games get features the same way, which is what the predict step uses.

Run after etl.py, the odds loaders and elo.py (Elo ratings are read from predictions).

    python features.py
    python features.py --league nfl
"""

import argparse
from collections import defaultdict

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

import nflverse
from database import closing_lines, connect, init_db
from espn_client import LEAGUES

# Efficiency stats come from play-by-play; the same tables also hold box scores (source 'box').
PBP_SOURCE = {"nfl": "nflverse_pbp", "cfb": "espn_pbp"}
EWM_HALFLIFE = 6    # games; form carries across seasons and fades
MAX_REST_DAYS = 21  # season openers and bye-week outliers are capped here
# Efficiency stats from team_game_stats (nflverse play-by-play) averaged into form, when present.
EFFICIENCY_STATS = ["off_epa", "def_epa", "off_success", "def_success", "off_pass_epa", "def_pass_epa",
                    "off_rush_epa", "def_rush_epa", "off_cpoe"]


def read_frame(conn, sql, params):
    cur = conn.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def load_games(conn, league):
    games = read_frame(
        conn,
        """
        SELECT g.game_id, g.season, g.season_type, g.week, g.start_time, g.completed,
               g.neutral_site, g.conference_game, g.home_team_id, g.away_team_id,
               g.home_score, g.away_score, g.home_rank, g.away_rank, g.venue_indoor,
               p.home_win_prob AS elo_prob, p.predicted_margin AS elo_margin,
               (p.details->>'home_rating')::float AS home_elo, (p.details->>'away_rating')::float AS away_elo
        FROM games g
        LEFT JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
        WHERE g.league = %s AND g.start_time IS NOT NULL
        ORDER BY g.start_time, g.game_id
        """,
        (league,),
    )
    if games["elo_prob"].isna().all():
        raise SystemExit(f"[{league}] no Elo predictions found; run elo.py first.")
    for col in ["home_score", "away_score", "home_rank", "away_rank", "week"]:
        games[col] = games[col].astype("Float64").astype(float)
    for col in ["neutral_site", "conference_game", "venue_indoor"]:
        games[col] = games[col].astype("boolean").astype("Float64").astype(float)
    games["start_time"] = pd.to_datetime(games["start_time"], utc=True)
    played = games["completed"] & games["home_score"].notna() & games["away_score"].notna()
    games["margin"] = (games["home_score"] - games["away_score"]).where(played)
    games["total_points"] = (games["home_score"] + games["away_score"]).where(played)
    return games


def load_lines(conn, league):
    lines = closing_lines(conn, league)
    return pd.DataFrame(
        [(game_id, l["spread"], l["total"], l["open_spread"], l["home_prob"]) for game_id, l in lines.items()],
        columns=["game_id", "line_spread", "line_total", "line_open_spread", "line_home_prob"],
    )


def load_nflverse(conn):
    """Game context nflverse has and ESPN doesn't: division games, roof, weather, starting QBs."""
    pairs = list(nflverse.matched_rows(conn, nflverse.stored_rows(conn)))
    df = pd.DataFrame([row for _, row in pairs])
    out = pd.DataFrame({"game_id": [game_id for game_id, _ in pairs]})
    out["div_game"] = pd.to_numeric(df["div_game"], errors="coerce")
    out["dome"] = df["roof"].isin(["dome", "closed"]).astype(float)
    out["temp"] = pd.to_numeric(df["temp"], errors="coerce")
    out["wind"] = pd.to_numeric(df["wind"], errors="coerce")
    out["home_qb"] = df["home_qb_id"].replace("", None)
    out["away_qb"] = df["away_qb_id"].replace("", None)
    return out


def team_games(games):
    """Long format: one row per team per game, from that team's side."""
    sides = []
    for side, opp in (("home", "away"), ("away", "home")):
        sign = 1 if side == "home" else -1
        sides.append(pd.DataFrame({
            "game_id": games["game_id"],
            "team": games[f"{side}_team_id"],
            "season": games["season"],
            "start_time": games["start_time"],
            "is_home": float(side == "home"),
            "points_for": games[f"{side}_score"].where(games["margin"].notna()),
            "points_against": games[f"{opp}_score"].where(games["margin"].notna()),
            "margin": sign * games["margin"],
            # How much better or worse than Elo expected: a strength signal Elo adjusts to slowly.
            "vs_elo": sign * (games["margin"] - games["elo_margin"]),
        }))
    return pd.concat(sides, ignore_index=True).sort_values(["start_time", "game_id"], kind="stable")


def load_team_stats(conn, league):
    rows = conn.execute(
        "SELECT game_id, team_id, stats FROM team_game_stats WHERE league = %s AND source = %s",
        (league, PBP_SOURCE[league]),
    ).fetchall()
    if not rows:
        return None
    columns = list(dict.fromkeys(EFFICIENCY_STATS + [f"off_{s}" for s in RIDGE_STATS]))
    stats = pd.DataFrame([r[2] for r in rows]).reindex(columns=columns).astype(float)
    stats.insert(0, "game_id", [r[0] for r in rows])
    stats.insert(1, "team", [r[1] for r in rows])
    return stats


# Fit as off_<stat> ~ offense[team] + defense[opponent]. Beyond overall efficiency, the
# supporting cast: pass protection (sack rate), receivers after the catch, and the run game.
RIDGE_STATS = ["epa", "success", "pass_epa", "rush_epa", "sack_rate", "yac_epa", "rush_success"]
RIDGE_WINDOW_DAYS = 600
# Per league, tuned on 2008-2021: (half-life in days, shrinkage toward average in weighted games).
RIDGE_PARAMS = {"nfl": (240, 8.0), "cfb": (120, 3.0)}


def ridge_ratings(long, halflife=240, shrinkage=8.0):
    """Opponent-adjusted offense/defense ratings going into each game day (SRS-style).

    Before each date, fits off_<stat> = mean + home + offense[team] + defense[opponent] by
    weighted ridge regression on games completed before that date, solving for every
    team's schedule strength at once. Returns one row per (game_id, team) with
    ridge_off_<stat> / ridge_def_<stat> (def = what the defense adds to opponents' offense).
    """
    done = long[long["margin"].notna() & long["off_epa"].notna()]
    teams = sorted(long["team"].unique())
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    # One observation per team-game offense: that team's offense against the opponent's defense.
    opp = done[["game_id", "team"]].merge(done[["game_id", "team"]], on="game_id", suffixes=("", "_opp"))
    opp = opp[opp["team"] != opp["team_opp"]]
    obs = done.merge(opp, on=["game_id", "team"])
    obs_time = obs["start_time"].dt.tz_convert(None).to_numpy()  # naive UTC datetime64
    X = np.zeros((len(obs), 2 + 2 * n))
    X[:, 0] = 1.0
    X[:, 1] = obs["is_home"].to_numpy() * 2 - 1  # +1 home, -1 away (neutral sites are rare in the NFL)
    X[np.arange(len(obs)), 2 + obs["team"].map(index).to_numpy()] = 1.0
    X[np.arange(len(obs)), 2 + n + obs["team_opp"].map(index).to_numpy()] = 1.0
    stats = [s for s in RIDGE_STATS if done[f"off_{s}"].notna().any()]  # CFB has no YAC data, etc.
    Y = obs[[f"off_{s}" for s in stats]].to_numpy()
    penalty = np.full(2 + 2 * n, shrinkage)
    penalty[:2] = 1e-6  # don't shrink the intercept or home edge

    out = []
    days = long["start_time"].dt.tz_convert("America/New_York").dt.normalize()
    for day, rows in long.groupby(days):
        cutoff = day.tz_convert("UTC").tz_localize(None).to_datetime64()
        age = (cutoff - obs_time) / np.timedelta64(1, "D")
        use = (age > 0) & (age <= RIDGE_WINDOW_DAYS) & ~np.isnan(Y).any(axis=1)
        if use.sum() < 100:
            continue
        w = 0.5 ** (age[use] / halflife)
        Xu = X[use]
        # Every stat shares the design matrix, so one solve fits them all.
        beta = np.linalg.solve(Xu.T @ (Xu * w[:, None]) + np.diag(penalty), Xu.T @ (Y[use] * w[:, None]))
        idx = rows["team"].map(index).to_numpy()
        record = pd.DataFrame({"game_id": rows["game_id"].to_numpy(), "team": rows["team"].to_numpy()})
        for k, stat in enumerate(stats):
            record[f"ridge_off_{stat}"] = beta[2 + idx, k]
            record[f"ridge_def_{stat}"] = beta[2 + n + idx, k]
        out.append(record)
    return pd.concat(out, ignore_index=True)


def form_features(long, extra_stats=()):
    """Per team-game features, computed only from games completed before kickoff."""
    done = long[long["margin"].notna()].copy().sort_values(["team", "start_time"], kind="stable")
    by_team = done.groupby("team", sort=False)

    # Post-game running stats: value after each completed game, including it.
    # ignore_na keeps a game with no efficiency data from diluting the average.
    for col in ["margin", "points_for", "points_against", "vs_elo", *extra_stats]:
        done[f"ewm_{col}"] = by_team[col].transform(lambda s: s.ewm(halflife=EWM_HALFLIFE, ignore_na=True).mean())
    done["last3_margin"] = by_team["margin"].transform(lambda s: s.rolling(3, min_periods=1).mean())
    by_season = done.groupby(["team", "season"], sort=False)
    done["season_games"] = by_season.cumcount() + 1
    done["season_win_pct"] = by_season["margin"].transform(lambda s: (s > 0).astype(float).expanding().mean())
    done["season_margin"] = by_season["margin"].transform(lambda s: s.expanding().mean())
    done["last_season"] = done["season"]

    stat_cols = ["ewm_margin", "ewm_points_for", "ewm_points_against", "ewm_vs_elo", "last3_margin",
                 *[f"ewm_{c}" for c in extra_stats],
                 "season_games", "season_win_pct", "season_margin", "last_season"]
    post = done[["team", "start_time", *stat_cols]].sort_values("start_time", kind="stable")

    # For every team-game (played or scheduled), take stats as of the latest earlier completed game.
    rows = long[["game_id", "team", "season", "start_time"]].sort_values("start_time", kind="stable")
    feats = pd.merge_asof(rows, post, on="start_time", by="team", allow_exact_matches=False)

    new_season = feats["last_season"] != feats["season"]
    feats.loc[new_season, ["season_games"]] = 0
    feats.loc[new_season, ["season_win_pct", "season_margin"]] = np.nan
    feats = feats.drop(columns=["last_season", "season"])

    # Rest counts any previous game, played or not; schedules are known ahead of time.
    all_games = long.sort_values(["team", "start_time"], kind="stable")
    prev = all_games.groupby("team")["start_time"].shift()
    rest = ((all_games["start_time"] - prev).dt.total_seconds() / 86400).clip(upper=MAX_REST_DAYS)
    feats = feats.merge(all_games.assign(rest_days=rest)[["game_id", "team", "rest_days"]], on=["game_id", "team"])
    return feats


# Per league, tuned on 2008-2021 (fit to the part of the margin Elo doesn't explain):
#   prior: EPA/dropback assumed for a QB with no history (backup level; in college an unknown
#          QB is usually a freshman or an FCS starter, so it sits lower on CFB's scale)
#   shrink: dropbacks of history before it outweighs the prior
#   halflife: days; a QB's track record stays informative for years
QB_PARAMS = {"nfl": {"prior": -0.15, "shrink": 100, "halflife": 1460},
             "cfb": {"prior": -0.45, "shrink": 30, "halflife": 1460}}


def load_qb_games(conn, league):
    rows = conn.execute(
        """
        SELECT p.player_id, p.game_id, p.team_id,
               CASE WHEN p.team_id = g.home_team_id THEN g.away_team_id ELSE g.home_team_id END,
               g.start_time, (p.stats->>'dropbacks')::float, (p.stats->>'epa_sum')::float
        FROM player_game_stats p JOIN games g USING (league, game_id)
        WHERE p.league = %s AND p.source = %s
        """,
        (league, PBP_SOURCE[league]),
    ).fetchall()
    qb_games = pd.DataFrame(rows, columns=["qb", "game_id", "team", "opponent", "start_time", "dropbacks", "epa_sum"])
    qb_games["start_time"] = pd.to_datetime(qb_games["start_time"], utc=True)
    return qb_games


def adjust_qb_games(qb_games, ridge):
    """Credit each QB game for the pass defense faced: subtract, per dropback, the opponent's
    pre-game ridge pass-defense rating (EPA/play it adds to offenses; negative = good defense)."""
    opp_def = ridge.set_index(["game_id", "team"])["ridge_def_pass_epa"]
    faced = opp_def.reindex(pd.MultiIndex.from_frame(qb_games[["game_id", "opponent"]])).to_numpy()
    adjusted = qb_games.copy()
    adjusted["epa_sum"] = qb_games["epa_sum"] - qb_games["dropbacks"] * np.nan_to_num(faced)
    return adjusted


def qb_ratings(games, starters_by_game, qb_games, prior=-0.15, shrink=100, halflife=1460):
    """Pre-game rating of each team's starting QB, from that QB's earlier games on any team.

    rating = (sum w*EPA + shrink*prior) / (sum w*dropbacks + shrink), w = 0.5^(days ago / halflife),
    using only games before kickoff. starters_by_game has (game_id, home_qb, away_qb); where a
    starter is missing (e.g. NFL games beyond the coming week) the team's most recent starter is assumed.
    Returns per (game_id, team): qb_rating, qb_experience (log weighted dropbacks), and
    qb_vs_prev (rating minus the rating of the team's previous starter, 0 if the same QB).
    """
    starters = pd.concat([
        games[["game_id", "start_time", f"{side}_team_id"]].rename(columns={f"{side}_team_id": "team"})
        .merge(starters_by_game[["game_id", f"{side}_qb"]].rename(columns={f"{side}_qb": "qb"}), on="game_id", how="left")
        for side in ("home", "away")
    ]).sort_values(["team", "start_time"], kind="stable").reset_index(drop=True)
    starters["qb"] = starters.groupby("team")["qb"].ffill()
    starters["prev_qb"] = starters.groupby("team")["qb"].shift()

    history = {
        qb: (g["start_time"].dt.tz_convert(None).to_numpy(), g["dropbacks"].to_numpy(), g["epa_sum"].to_numpy())
        for qb, g in qb_games.groupby("qb")
    }

    def rate(qb, when):
        if not isinstance(qb, str) or qb not in history:
            return prior, 0.0
        times, dropbacks, epa = history[qb]
        age = (when - times) / np.timedelta64(1, "D")
        w = np.where(age > 0, 0.5 ** (age / halflife), 0.0)
        weighted_db = float(w @ dropbacks)
        return (float(w @ epa) + shrink * prior) / (weighted_db + shrink), weighted_db

    kickoff = starters["start_time"].dt.tz_convert(None).to_numpy()
    rated = [rate(qb, t) for qb, t in zip(starters["qb"], kickoff)]
    prev = [rate(qb, t)[0] for qb, t in zip(starters["prev_qb"], kickoff)]
    starters["qb_rating"] = [r for r, _ in rated]
    starters["qb_experience"] = np.log1p([d for _, d in rated])
    starters["qb_vs_prev"] = np.where(
        starters["prev_qb"].isna() | (starters["qb"] == starters["prev_qb"]), 0.0, starters["qb_rating"] - prev
    )
    return starters[["game_id", "team", "qb_rating", "qb_experience", "qb_vs_prev"]]


RECRUITING_CLASSES = 4  # recruiting strength = average of the last four signing classes


def preseason_context(conn, league, games):
    """Per (game_id, team): what was known about the team before its season started.

    Previous season's final SP+ (the current season's SP+ updates with games already played,
    so it would leak), the four-class recruiting average, the talent composite and returning
    production (all published before the season).
    """
    rows = conn.execute("SELECT season, team_id, stats FROM team_seasons WHERE league = %s", (league,)).fetchall()
    if not rows:
        return None
    seasons = pd.DataFrame([{"season": r[0], "team": r[1], **r[2]} for r in rows])
    seasons = seasons.reindex(columns=["season", "team", "sp_rating", "sp_offense", "sp_defense", "recruiting_points",
                                       "talent", "returning_ppa_pct", "returning_passing_ppa_pct"])
    by_team = seasons.set_index(["team", "season"]).sort_index()

    teams = pd.concat([
        games[["game_id", "season", f"{side}_team_id"]].rename(columns={f"{side}_team_id": "team"}) for side in ("home", "away")
    ])
    keys = list(zip(teams["team"], teams["season"]))
    prev = by_team[["sp_rating", "sp_offense", "sp_defense"]].reindex([(t, s - 1) for t, s in keys])
    current = by_team[["talent", "returning_ppa_pct", "returning_passing_ppa_pct"]].reindex(keys)
    recruiting = by_team["recruiting_points"]
    classes = np.column_stack([recruiting.reindex([(t, s - k) for t, s in keys]).to_numpy()
                               for k in range(RECRUITING_CLASSES)])
    counts = (~np.isnan(classes)).sum(axis=1)
    recruiting_avg = np.where(counts > 0, np.nansum(classes, axis=1) / np.maximum(counts, 1), np.nan)
    return pd.DataFrame({
        "game_id": teams["game_id"].to_numpy(), "team": teams["team"].to_numpy(),
        "prev_sp_rating": prev["sp_rating"].to_numpy(), "prev_sp_offense": prev["sp_offense"].to_numpy(),
        "prev_sp_defense": prev["sp_defense"].to_numpy(), "recruiting_avg": recruiting_avg,
        "talent": current["talent"].to_numpy(), "returning_ppa_pct": current["returning_ppa_pct"].to_numpy(),
        "returning_passing_ppa_pct": current["returning_passing_ppa_pct"].to_numpy(),
    })


PRESEASON_MISSING = -25.0  # teams without a preseason rating (FCS, first FBS season) are rated well below FBS


AVAILABILITY = ["starters_out_off", "starters_out_def", "ol_out", "skill_out", "front_out", "db_out",
                "starters_questionable", "missing_off_prod", "missing_def_prod"]


def availability(conn, league):
    """Per (game_id, team): injury-report availability (nflverse_availability.py), NFL only."""
    rows = conn.execute("SELECT game_id, team_id, stats FROM team_game_stats WHERE league = %s AND source = 'availability'",
                        (league,)).fetchall()
    if not rows:
        return None
    frame = pd.DataFrame([r[2] for r in rows]).reindex(columns=AVAILABILITY).astype(float)
    frame.insert(0, "game_id", [r[0] for r in rows])
    frame.insert(1, "team", [r[1] for r in rows])
    return frame


def preseason_ratings(conn, league, games):
    """Per (game_id, team): the team's preseason rating (offseason.py) for the game's season.
    Seasons with no ratings stay NaN; teams missing in a rated season (CFB: FCS, first FBS
    season) get PRESEASON_MISSING."""
    rows = conn.execute("SELECT season, team_id, rating FROM team_preseason WHERE league = %s", (league,)).fetchall()
    if not rows:
        return None
    ratings = {(s, t): r for s, t, r in rows}
    rated_seasons = {s for s, _, _ in rows}
    teams = pd.concat([
        games[["game_id", "season", f"{side}_team_id"]].rename(columns={f"{side}_team_id": "team"}) for side in ("home", "away")
    ])
    teams["preseason_rating"] = [ratings.get((s, t), PRESEASON_MISSING) if s in rated_seasons else np.nan
                                 for s, t in zip(teams["season"], teams["team"])]
    return teams[["game_id", "team", "preseason_rating"]]


def previous_starters(games, qb_games):
    """Pre-game starter guess where no source lists starters (CFB): the QB with the most
    dropbacks in the team's previous game. Returns a frame shaped like load_nflverse's
    (game_id, home_qb, away_qb)."""
    primary = (qb_games.sort_values("dropbacks", ascending=False)
               .drop_duplicates(["game_id", "team"])[["game_id", "team", "qb"]])
    sides = []
    for side in ("home", "away"):
        team_games = games[["game_id", "start_time", f"{side}_team_id"]].rename(columns={f"{side}_team_id": "team"})
        sides.append(team_games.assign(side=side))
    rows = pd.concat(sides).merge(primary, on=["game_id", "team"], how="left")
    rows = rows.sort_values(["team", "start_time"], kind="stable")
    rows["qb"] = rows.groupby("team")["qb"].transform(lambda s: s.ffill().shift())
    wide = rows.pivot(index="game_id", columns="side", values="qb")
    return pd.DataFrame({"game_id": wide.index, "home_qb": wide.get("home"), "away_qb": wide.get("away")}).reset_index(drop=True)


NEWS_OUT = {"out", "doubtful", "suspended", "season-ending"}
NEWS_WINDOW_DAYS = 21  # a report counts for this long (season-ending: the rest of the season)


def news_qb_overrides(conn, games, starters, qb_games):
    """CFB: if news (cfb_news.py) says a team's presumed starting QB is out before a game, use the
    backup: the team's QB with the most dropbacks earlier that season, else last season (no
    history -> the QB prior). A later 'returning' / 'probable' / 'questionable' report cancels it.
    Uses the already-learned QB-rating effect; there is no injury-news history to learn from."""
    reports = conn.execute(
        "SELECT team_id, player_name, status, published FROM player_status "
        "WHERE league = 'cfb' AND source = 'news_llm' ORDER BY published").fetchall()
    if not reports:
        return starters, 0
    from cfb_epa import qb_key  # QBs are keyed by first initial + last name within a team
    by_team = defaultdict(list)
    for team_id, name, status, published in reports:
        by_team[team_id].append((pd.Timestamp(published), qb_key(name), status))
    kickoff = games.set_index("game_id")["start_time"]
    season = games.set_index("game_id")["season"]
    teams = {"home": games.set_index("game_id")["home_team_id"], "away": games.set_index("game_id")["away_team_id"]}
    qb = qb_games
    changed = 0
    starters = starters.copy()
    for idx, row in starters.iterrows():
        game_id = row["game_id"]
        for side in ("home", "away"):
            key = row[f"{side}_qb"]
            team = teams[side].get(game_id)
            if not isinstance(key, str) or team not in by_team:
                continue
            start = kickoff[game_id]
            latest = None
            for published, name, status in by_team[team]:
                if published >= start or name != key.split(":", 1)[1]:
                    continue
                if status == "season-ending" and published.year == start.year or \
                        (start - published).days <= NEWS_WINDOW_DAYS:
                    latest = status
            if latest not in NEWS_OUT:
                continue
            others = qb[(qb["team"] == team) & (qb["qb"] != key) & (qb["start_time"] < start)]
            this_season = others[others["start_time"].dt.year >= season[game_id]]
            pool = this_season if len(this_season) else others[others["start_time"].dt.year >= season[game_id] - 1]
            backup = pool.groupby("qb")["dropbacks"].sum().idxmax() if len(pool) else None
            starters.at[idx, f"{side}_qb"] = backup if backup is not None else f"{team}:unknown backup"
            changed += 1
    return starters, changed


def qb_changed(games, nfl):
    """1 if a team's starting QB differs from its previous game's starter (NFL only)."""
    starters = pd.concat([
        games[["game_id", "start_time", f"{side}_team_id"]].rename(columns={f"{side}_team_id": "team"})
        .merge(nfl[["game_id", f"{side}_qb"]].rename(columns={f"{side}_qb": "qb"}), on="game_id")
        for side in ("home", "away")
    ]).sort_values(["team", "start_time"], kind="stable")
    prev = starters.groupby("team")["qb"].shift()
    starters["qb_changed"] = np.where(starters["qb"].isna() | prev.isna(), np.nan, (starters["qb"] != prev).astype(float))
    return starters[["game_id", "team", "qb_changed"]]


def build(conn, league):
    games = load_games(conn, league)
    long = team_games(games)
    team_stats = load_team_stats(conn, league)
    extra_stats = []
    if team_stats is not None:
        long = long.merge(team_stats, on=["game_id", "team"], how="left")
        extra_stats = EFFICIENCY_STATS
    form = form_features(long, extra_stats)
    ridge = ridge_ratings(long, *RIDGE_PARAMS[league]) if team_stats is not None else None
    if ridge is not None:
        form = form.merge(ridge, on=["game_id", "team"], how="left")

    extra = {}
    qb_games = load_qb_games(conn, league)
    if league == "nfl":
        nfl = load_nflverse(conn)
        form = form.merge(qb_changed(games, nfl), on=["game_id", "team"], how="left")
        starters = nfl
        extra = nfl.drop(columns=["home_qb", "away_qb"])
    else:
        starters = previous_starters(games, qb_games) if len(qb_games) else None
        if starters is not None:
            starters, swapped = news_qb_overrides(conn, games, starters, qb_games)
            if swapped:
                print(f"[{league}] news: {swapped} starting-QB absences applied (backup's rating used)", flush=True)
    if len(qb_games) and starters is not None:
        rated_games = adjust_qb_games(qb_games, ridge) if ridge is not None else qb_games
        form = form.merge(qb_ratings(games, starters, rated_games, **QB_PARAMS[league]),
                          on=["game_id", "team"], how="left")

    context = preseason_context(conn, league, games)
    if context is not None:
        form = form.merge(context, on=["game_id", "team"], how="left")
    preseason = preseason_ratings(conn, league, games)
    if preseason is not None:
        form = form.merge(preseason, on=["game_id", "team"], how="left")
    injuries = availability(conn, league)
    if injuries is not None:
        form = form.merge(injuries, on=["game_id", "team"], how="left")

    team_cols = [c for c in form.columns if c not in ("game_id", "team", "start_time")]
    df = games.copy()
    for side in ("home", "away"):
        side_form = form.rename(columns={c: f"{side}_{c}" for c in team_cols})
        side_form = side_form.rename(columns={"team": f"{side}_team_id"}).drop(columns="start_time")
        df = df.merge(side_form, on=["game_id", f"{side}_team_id"], how="left")
    derived = {f"diff_{col}": df[f"home_{col}"] - df[f"away_{col}"] for col in team_cols}
    derived["elo_diff"] = df["home_elo"] - df["away_elo"]
    if extra_stats:
        # Offense against the defense it faces (def_epa is EPA allowed, so higher = worse defense).
        derived["home_matchup_epa"] = df["home_ridge_off_epa"] + df["away_ridge_def_epa"]
        derived["away_matchup_epa"] = df["away_ridge_off_epa"] + df["home_ridge_def_epa"]
        derived["diff_matchup_epa"] = derived["home_matchup_epa"] - derived["away_matchup_epa"]
        for stat in [s for s in RIDGE_STATS if f"diff_ridge_off_{s}" in derived]:
            derived[f"diff_ridge_net_{stat}"] = derived[f"diff_ridge_off_{stat}"] - derived[f"diff_ridge_def_{stat}"]
    df = pd.concat([df, pd.DataFrame(derived)], axis=1)

    if len(extra):
        df = df.merge(extra, on="game_id", how="left")
    df = df.merge(load_lines(conn, league), on="game_id", how="left")

    feature_cols = [
        "elo_prob", "elo_margin", "home_elo", "away_elo", "elo_diff",
        "season_type", "week", "neutral_site", "conference_game", "venue_indoor", "home_rank", "away_rank",
        *[f"{p}_{c}" for p in ("home", "away", "diff") for c in team_cols],
        *[c for c in ("home_matchup_epa", "away_matchup_epa", "diff_matchup_epa") if c in df],
        *[f"diff_ridge_net_{stat}" for stat in RIDGE_STATS if f"diff_ridge_net_{stat}" in df],
        *[c for c in ("div_game", "dome", "temp", "wind") if c in df],
        "line_spread", "line_total", "line_open_spread", "line_home_prob",
    ]
    return df, feature_cols


def write(conn, league, df, feature_cols):
    def clean(record):
        return {k: (None if pd.isna(v) else float(v)) for k, v in record.items()}

    features = df[feature_cols].astype(float).to_dict("records")
    rows = [
        (league, g.game_id, int(g.season), g.start_time.to_pydatetime(), bool(g.completed),
         None if pd.isna(g.margin) else float(g.margin),
         None if pd.isna(g.total_points) else float(g.total_points), Jsonb(clean(f)))
        for g, f in zip(df.itertuples(), features)
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM game_features WHERE league = %s", (league,))
        cur.executemany(
            "INSERT INTO game_features (league, game_id, season, start_time, completed, margin, total_points, features) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    print(f"[{league}] {len(rows)} games x {len(feature_cols)} features written", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Build pre-game feature rows into game_features.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            df, cols = build(conn, league)
            write(conn, league, df, cols)


if __name__ == "__main__":
    main()
