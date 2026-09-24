"""Build one feature row per game from information available before kickoff.

Team form features come from each team's most recent game that finished *before* this
kickoff (a merge_asof on start time), so no row can see its own result or anything
later. Scheduled games get features the same way, which is what the predict step uses.

Run after etl.py, the odds loaders and elo.py (Elo ratings are read from predictions).

    python features.py
    python features.py --league nfl
"""

import argparse

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

import nflverse
from database import closing_lines, connect, init_db
from espn_client import LEAGUES

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
        "SELECT game_id, team_id, stats FROM team_game_stats WHERE league = %s", (league,)
    ).fetchall()
    if not rows:
        return None
    stats = pd.DataFrame([r[2] for r in rows]).reindex(columns=EFFICIENCY_STATS).astype(float)
    stats.insert(0, "game_id", [r[0] for r in rows])
    stats.insert(1, "team", [r[1] for r in rows])
    return stats


RIDGE_STATS = ["epa", "success"]  # fit as off_<stat> ~ offense[team] + defense[opponent]
RIDGE_HALFLIFE_DAYS = 240        # tuned on 2008-2021; a game a year back counts about a third
RIDGE_WINDOW_DAYS = 600
RIDGE_LAMBDA = 8.0               # shrinkage toward average, in (weighted) games


def ridge_ratings(long):
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
    Y = obs[[f"off_{s}" for s in RIDGE_STATS]].to_numpy()
    penalty = np.full(2 + 2 * n, RIDGE_LAMBDA)
    penalty[:2] = 1e-6  # don't shrink the intercept or home edge

    out = []
    days = long["start_time"].dt.tz_convert("America/New_York").dt.normalize()
    for day, rows in long.groupby(days):
        cutoff = day.tz_convert("UTC").tz_localize(None).to_datetime64()
        age = (cutoff - obs_time) / np.timedelta64(1, "D")
        use = (age > 0) & (age <= RIDGE_WINDOW_DAYS) & ~np.isnan(Y).any(axis=1)
        if use.sum() < 100:
            continue
        w = 0.5 ** (age[use] / RIDGE_HALFLIFE_DAYS)
        Xu = X[use]
        beta = np.linalg.solve(Xu.T @ (Xu * w[:, None]) + np.diag(penalty), Xu.T @ (Y[use] * w[:, None]))
        idx = rows["team"].map(index).to_numpy()
        record = pd.DataFrame({"game_id": rows["game_id"].to_numpy(), "team": rows["team"].to_numpy()})
        for k, stat in enumerate(RIDGE_STATS):
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
    if team_stats is not None:
        form = form.merge(ridge_ratings(long), on=["game_id", "team"], how="left")

    extra = {}
    if league == "nfl":
        nfl = load_nflverse(conn)
        form = form.merge(qb_changed(games, nfl), on=["game_id", "team"], how="left")
        extra = nfl.drop(columns=["home_qb", "away_qb"])

    team_cols = [c for c in form.columns if c not in ("game_id", "team", "start_time")]
    df = games.copy()
    for side in ("home", "away"):
        side_form = form.rename(columns={c: f"{side}_{c}" for c in team_cols})
        side_form = side_form.rename(columns={"team": f"{side}_team_id"}).drop(columns="start_time")
        df = df.merge(side_form, on=["game_id", f"{side}_team_id"], how="left")
    for col in team_cols:
        df[f"diff_{col}"] = df[f"home_{col}"] - df[f"away_{col}"]
    df["elo_diff"] = df["home_elo"] - df["away_elo"]
    if extra_stats:
        # Offense against the defense it faces (def_epa is EPA allowed, so higher = worse defense).
        df["home_matchup_epa"] = df["home_ridge_off_epa"] + df["away_ridge_def_epa"]
        df["away_matchup_epa"] = df["away_ridge_off_epa"] + df["home_ridge_def_epa"]
        df["diff_matchup_epa"] = df["home_matchup_epa"] - df["away_matchup_epa"]
        for stat in RIDGE_STATS:
            df[f"diff_ridge_net_{stat}"] = df[f"diff_ridge_off_{stat}"] - df[f"diff_ridge_def_{stat}"]

    if len(extra):
        df = df.merge(extra, on="game_id", how="left")
    df = df.merge(load_lines(conn, league), on="game_id", how="left")

    feature_cols = [
        "elo_prob", "elo_margin", "home_elo", "away_elo", "elo_diff",
        "season_type", "week", "neutral_site", "conference_game", "venue_indoor", "home_rank", "away_rank",
        *[f"{p}_{c}" for p in ("home", "away", "diff") for c in team_cols],
        *[c for c in ("home_matchup_epa", "away_matchup_epa", "diff_matchup_epa",
                      "diff_ridge_net_epa", "diff_ridge_net_success") if c in df],
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
