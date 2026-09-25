"""How offseason player movement relates to a team's next season, for college and the NFL.

For each team-season this builds:
    target      season strength: SRS (points better than an average team, neutral field)
                CFB: FBS-vs-FBS games incl. bowls; NFL: regular season
    history     last season's SRS and offense/defense ratings, two seasons back, last season's EPA
    movement    per production category (passing, rushing, receiving, tackles, havoc = TFL + sacks
                + INT + passes defended), as shares of team production:
                  returning  last season's share from players back on the same roster
                  incoming   newcomers' share of their old team's production, weighted by the old
                             team's strength vs the new team's
                               CFB: transfer portal (2021+)   NFL: free agents / trades (roster moves)
                plus CFB recruiting, talent and 247 transfer ratings; NFL draft capital;
                and a new-head-coach flag
Returning offense/defense scale how much of last season's offense/defense carries over.

A ridge regression maps these to the season's SRS. It's evaluated on seasons it wasn't fit on
(a forward split, and leave-one-season-out) against last season's SRS alone. Preseason ratings
are then written for every season, each fit only on earlier seasons, with the rating broken
into contribution groups for the site.

    python offseason.py                 # both leagues
    python offseason.py --league nfl
"""

import argparse
from collections import defaultdict

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from database import connect, init_db

CATEGORIES = {
    "passing": ["pass_yds"],
    "rush": ["rush_yds"],
    "rec": ["rec_yds"],
    "tackles": ["tackles"],
    "havoc": ["tfl", "sacks", "def_int", "pass_def"],
}
FCS_SRS = -25.0         # CFB: strength assumed for a non-FBS team
SRS_SHRINK = 2.0        # ridge penalty (in games) when fitting season ratings
FIRST_SEASON = 2006     # needs the previous season's production
EPA_SOURCE = {"cfb": "espn_pbp", "nfl": "nflverse_pbp"}

HISTORY = ["prev_srs", "prev2_srs", "prev_off_epa", "prev_def_epa"]
MOVEMENT = ["prev_off", "prev_def", "prev_off_x_ret", "prev_def_x_ret", "ret_off", "ret_def", "in_off", "in_def"]
CONFIG = {
    "cfb": {
        "context": HISTORY + ["recruiting", "talent", "new_coach"],
        "movement": MOVEMENT + ["transfer_in_rating", "transfer_out_rating"],
        "groups": {"history": HISTORY + ["prev_off", "prev_def"], "recruiting": ["recruiting", "talent"],
                   "returning": ["ret_off", "ret_def", "prev_off_x_ret", "prev_def_x_ret"],
                   "transfers": ["in_off", "in_def", "transfer_in_rating", "transfer_out_rating"],
                   "coaching": ["new_coach"]},
        "forward_split": 2022,     # fit through, test after
        "loso_from": 2021,         # leave-one-season-out over the portal era
        "first_rated": 2009,
    },
    "nfl": {
        "context": HISTORY + ["new_coach"],
        "movement": MOVEMENT + ["draft_capital"],
        # Same group keys as college so the site can share code; the NFL labels them
        # "draft" (recruiting) and "free agency & trades" (transfers).
        "groups": {"history": HISTORY + ["prev_off", "prev_def"], "recruiting": ["draft_capital"],
                   "returning": ["ret_off", "ret_def", "prev_off_x_ret", "prev_def_x_ret"],
                   "transfers": ["in_off", "in_def"], "coaching": ["new_coach"]},
        "forward_split": 2020,
        "loso_from": 2016,
        "first_rated": 2010,
    },
}


def features_for(league):
    return CONFIG[league]["context"] + CONFIG[league]["movement"]


def query_frame(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def rating_games(conn, league):
    """Games that define season strength: CFB FBS-vs-FBS (incl. bowls), NFL regular season."""
    if league == "cfb":
        return query_frame(conn, """
            SELECT g.season, g.home_team_id, g.away_team_id, g.home_score, g.away_score, g.neutral_site
            FROM games g
            JOIN team_affiliations h ON h.league = g.league AND h.season = g.season AND h.team_id = g.home_team_id
            JOIN team_affiliations a ON a.league = g.league AND a.season = g.season AND a.team_id = g.away_team_id
            WHERE g.league = 'cfb' AND g.completed AND h.classification = 'fbs' AND a.classification = 'fbs'""")
    return query_frame(conn, """
        SELECT season, home_team_id, away_team_id, home_score, away_score, neutral_site FROM games
        WHERE league = 'nfl' AND completed AND season_type = 2""")


def season_srs(games):
    """{(season, team_id): SRS}: margin = home edge + rating[home] - rating[away] (margins capped at 35)."""
    out = {}
    for season, g in games.groupby("season"):
        teams = sorted(set(g["home_team_id"]) | set(g["away_team_id"]))
        idx = {t: i for i, t in enumerate(teams)}
        X = np.zeros((len(g), len(teams) + 1))
        rows = np.arange(len(g))
        X[rows, g["home_team_id"].map(idx)] = 1
        X[rows, g["away_team_id"].map(idx)] = -1
        X[:, -1] = np.where(g["neutral_site"].fillna(False).astype(bool), 0, 1)
        penalty = np.full(len(teams) + 1, SRS_SHRINK)
        penalty[-1] = 1e-6
        y = (g["home_score"] - g["away_score"]).clip(-35, 35).to_numpy(float)
        beta = np.linalg.solve(X.T @ X + np.diag(penalty), X.T @ y)
        for t, i in idx.items():
            out[(season, t)] = beta[i]
    return out


def season_off_def(games):
    """{(season, team_id): (offense, defense)}: points scored above average and allowed below
    average vs an average opponent (so offense + defense ~ SRS)."""
    out = {}
    for season, g in games.groupby("season"):
        teams = sorted(set(g["home_team_id"]) | set(g["away_team_id"]))
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        rows = []
        for r in g.itertuples(index=False):
            home = 0.0 if r.neutral_site else 1.0
            rows.append((idx[r.home_team_id], idx[r.away_team_id], home, min(r.home_score, 63)))
            rows.append((idx[r.away_team_id], idx[r.home_team_id], -home, min(r.away_score, 63)))
        X = np.zeros((len(rows), 2 * n + 2))
        y = np.zeros(len(rows))
        for i, (off, dfn, home, pts) in enumerate(rows):
            X[i, off], X[i, n + dfn], X[i, -2], X[i, -1], y[i] = 1, -1, home / 2, 1, pts
        penalty = np.full(2 * n + 2, SRS_SHRINK)
        penalty[-2:] = 1e-6
        beta = np.linalg.solve(X.T @ X + np.diag(penalty), X.T @ y)
        for t, i in idx.items():
            out[(season, t)] = (beta[i], beta[n + i])
    return out


def player_shares(conn, league):
    """Per (season, team, player): share of the team's season production in each category."""
    season_types = (2,) if league == "nfl" else (2, 3)
    rows = conn.execute("""
        SELECT g.season, s.team_id, s.player_id, s.stats FROM player_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = %s AND s.source = 'box' AND g.season_type = ANY(%s)""", (league, list(season_types))).fetchall()
    totals = defaultdict(lambda: defaultdict(float))
    for season, team, player, stats in rows:
        for cat, keys in CATEGORIES.items():
            v = sum(stats.get(k) or 0 for k in keys)
            if v > 0:
                totals[(season, team, player)][cat] += v
    df = pd.DataFrame([{"season": k[0], "team": k[1], "player": k[2], **v} for k, v in totals.items()]).fillna(0.0)
    team_totals = df.groupby(["season", "team"])[list(CATEGORIES)].transform("sum").replace(0, np.nan)
    shares = df[list(CATEGORIES)] / team_totals
    return pd.concat([df[["season", "team", "player"]], shares.fillna(0.0)], axis=1)


def incoming_moves(conn, league, last, roster):
    """Newcomers with their production at their old team last season: season, player, origin, team."""
    cats = list(CATEGORIES)
    if league == "cfb":
        transfers = query_frame(conn, "SELECT season, player_id AS player, origin_team_id AS origin, "
                                      "dest_team_id AS team FROM transfers WHERE league = 'cfb'")
        return transfers.dropna(subset=["player", "team"]).merge(
            last.drop(columns="team").groupby(["season", "player"], as_index=False)[cats].sum(), on=["season", "player"])
    # NFL: on this season's roster for one team, produced for a different team last season.
    moved = roster.merge(last.rename(columns={"team": "origin"}), on=["season", "player"])
    return moved[moved["team"] != moved["origin"]]


def build_features(conn, league):
    games = rating_games(conn, league)
    srs = season_srs(games)
    od = season_off_def(games)
    srs_frame = pd.DataFrame([(s, t, v) for (s, t), v in srs.items()], columns=["season", "team", "srs"])
    shares = player_shares(conn, league)
    cats = list(CATEGORIES)
    roster = query_frame(conn, "SELECT DISTINCT season, team_id AS team, player_id AS player FROM rosters "
                               "WHERE league = %s", (league,))
    if league == "cfb":
        teams = query_frame(conn, "SELECT season, team_id AS team FROM team_affiliations "
                                  "WHERE league = 'cfb' AND classification = 'fbs' AND season >= %s", (FIRST_SEASON,))
    else:
        teams = query_frame(conn, "SELECT DISTINCT season, home_team_id AS team FROM games "
                                  "WHERE league = 'nfl' AND season >= %s", (FIRST_SEASON,))
    missing = FCS_SRS if league == "cfb" else 0.0

    df = teams.merge(srs_frame, on=["season", "team"], how="left")
    prev = srs_frame.assign(season=srs_frame["season"] + 1).rename(columns={"srs": "prev_srs"})
    df = df.merge(prev, on=["season", "team"], how="left")
    df["prev_srs"] = df["prev_srs"].fillna(missing)

    # Returning: last season's shares of players on this season's roster for the same team.
    last = shares.assign(season=shares["season"] + 1)
    back = last.merge(roster, on=["season", "team", "player"]).groupby(["season", "team"])[cats].sum()
    df = df.merge(back.add_prefix("ret_").reset_index(), on=["season", "team"], how="left")

    # Incoming: newcomers' share of their old team's production, weighted by the old team's level.
    moved = incoming_moves(conn, league, last, roster)
    origin_srs = srs_frame.assign(season=srs_frame["season"] + 1).rename(columns={"team": "origin", "srs": "origin_srs"})
    moved = moved.merge(origin_srs, on=["season", "origin"], how="left").merge(
        df[["season", "team", "prev_srs"]], on=["season", "team"], how="left")
    level = np.clip(1 + (moved["origin_srs"].fillna(missing) - moved["prev_srs"].fillna(0)) / 30, 0.2, 1.5)
    incoming_level = moved[cats].mul(level, axis=0).assign(season=moved["season"], team=moved["team"]) \
        .groupby(["season", "team"])[cats].sum().add_prefix("in_level_")
    df = df.merge(incoming_level.reset_index(), on=["season", "team"], how="left")
    df["n_incoming"] = df.merge(moved.groupby(["season", "team"]).size().rename("n").reset_index(),
                                on=["season", "team"], how="left")["n"].fillna(0).to_numpy()

    if league == "cfb":
        transfers = query_frame(conn, "SELECT season, origin_team_id AS origin, dest_team_id AS team, rating "
                                      "FROM transfers WHERE league = 'cfb'")
        t = transfers.assign(value=transfers["rating"].fillna(0.80) - 0.80)
        df = df.merge(t.dropna(subset=["team"]).groupby(["season", "team"]).agg(
            transfer_in_rating=("value", "sum"), n_transfers_in=("value", "size")).reset_index(),
            on=["season", "team"], how="left")
        df = df.merge(t.drop(columns="team").rename(columns={"origin": "team"}).dropna(subset=["team"])
                      .groupby(["season", "team"]).agg(transfer_out_rating=("value", "sum"),
                                                       n_transfers_out=("value", "size")).reset_index(),
                      on=["season", "team"], how="left")
        context = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT season, team_id, stats FROM team_seasons WHERE league = 'cfb' AND source = 'cfbd'")}
        df["talent"] = [(context.get((s, t)) or {}).get("talent") for s, t in zip(df["season"], df["team"])]
        classes = [[(context.get((s - k, t)) or {}).get("recruiting_points") for k in range(4)]
                   for s, t in zip(df["season"], df["team"])]
        df["recruiting"] = [np.mean([c for c in cs if c is not None]) if any(c is not None for c in cs) else np.nan
                            for cs in classes]
    else:
        # Draft capital: sum of ln(257 / pick) over the team's picks this spring.
        picks = query_frame(conn, "SELECT season, team_id AS team, pick FROM draft_picks WHERE league = 'nfl'")
        picks["capital"] = np.log(257 / picks["pick"].clip(lower=1))
        df = df.merge(picks.groupby(["season", "team"])["capital"].sum().rename("draft_capital").reset_index(),
                      on=["season", "team"], how="left")
        df["n_transfers_in"], df["n_transfers_out"] = df["n_incoming"], np.nan

    movement = [c for c in df.columns if c.startswith(("ret_", "in_level_", "transfer_", "draft_capital"))]
    df[movement] = df[movement].fillna(0.0)

    coaches = {(s, t): c for s, t, c in conn.execute(
        "SELECT season, team_id, coach FROM head_coaches WHERE league = %s", (league,))}
    df["new_coach"] = [float(coaches.get((s, t)) is not None and coaches.get((s - 1, t)) is not None
                             and coaches[(s, t)] != coaches[(s - 1, t)]) for s, t in zip(df["season"], df["team"])]

    df["prev_off"] = [od.get((s - 1, t), (missing / 2, missing / 2))[0] for s, t in zip(df["season"], df["team"])]
    df["prev_def"] = [od.get((s - 1, t), (missing / 2, missing / 2))[1] for s, t in zip(df["season"], df["team"])]
    df["prev2_srs"] = [srs.get((s - 2, t), missing) for s, t in zip(df["season"], df["team"])]
    epa = query_frame(conn, """
        SELECT g.season + 1 AS season, s.team_id AS team,
               sum((s.stats->>'off_epa')::float * (s.stats->>'off_plays')::float) / nullif(sum((s.stats->>'off_plays')::float), 0) AS prev_off_epa,
               sum((s.stats->>'def_epa')::float * (s.stats->>'def_plays')::float) / nullif(sum((s.stats->>'def_plays')::float), 0) AS prev_def_epa
        FROM team_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = %s AND s.source = %s GROUP BY 1, 2""", (league, EPA_SOURCE[league]))
    df = df.merge(epa, on=["season", "team"], how="left")
    # Passing counts double on offense (the QB); havoc double on defense.
    df["ret_off"] = (2 * df["ret_passing"] + df["ret_rush"] + df["ret_rec"]) / 4
    df["ret_def"] = (df["ret_tackles"] + 2 * df["ret_havoc"]) / 3
    df["in_off"] = (2 * df["in_level_passing"] + df["in_level_rush"] + df["in_level_rec"]) / 4
    df["in_def"] = (df["in_level_tackles"] + 2 * df["in_level_havoc"]) / 3
    df["prev_off_x_ret"] = df["prev_off"] * df["ret_off"]
    df["prev_def_x_ret"] = df["prev_def"] * df["ret_def"]
    return df.rename(columns={"team": "team_id"})


def model(alpha=10.0):
    return make_pipeline(StandardScaler(), Ridge(alpha=alpha))


def prepare(df, cols):
    """Fill gaps with the column mean (0 if a column has no data yet, e.g. talent before 2015)."""
    X = df[cols].astype(float)
    return X.fillna(X.mean()).fillna(0.0)


def evaluate(league, df):
    cfg = CONFIG[league]
    labeled = df[df["srs"].notna()]
    results = {}
    for name, cols in (("baseline: last season", ["prev_srs"]), ("+ history/context/coach", cfg["context"]),
                       ("+ offseason movement", features_for(league))):
        train = labeled[labeled["season"] <= cfg["forward_split"]]
        test = labeled[labeled["season"] > cfg["forward_split"]]
        pred = model().fit(prepare(train, cols), train["srs"]).predict(prepare(test, cols))
        fwd = (np.mean(np.abs(pred - test["srs"])), np.corrcoef(pred, test["srs"])[0, 1] ** 2)
        errs = []
        for season in range(cfg["loso_from"], int(labeled["season"].max()) + 1):
            tr, te = labeled[labeled["season"] != season], labeled[labeled["season"] == season]
            if te.empty:
                continue
            errs.append(np.abs(model().fit(prepare(tr, cols), tr["srs"]).predict(prepare(te, cols)) - te["srs"]))
        results[name] = (fwd[0], fwd[1], float(np.mean(np.concatenate(errs))))
    return results


def preseason_ratings(league, df):
    """For each season, fit on earlier seasons only and rate every team before it plays.
    Contributions: each group's points vs an average team (coaching vs keeping the same coach)."""
    cfg, features = CONFIG[league], features_for(league)
    out = []
    for season in sorted(df["season"].unique()):
        if season < cfg["first_rated"]:
            continue
        train = df[(df["season"] < season) & df["srs"].notna()]
        now = df[df["season"] == season]
        X_train = prepare(train, features)
        m = model().fit(X_train, train["srs"])
        X_now = now[features].astype(float).fillna(X_train.mean()).fillna(0.0)
        scaler, ridge = m[0], m[-1]
        parts = (X_now - scaler.mean_) / scaler.scale_ * ridge.coef_
        ratings = ridge.intercept_ + parts.sum(axis=1)
        i = features.index("new_coach")
        parts["new_coach"] = parts["new_coach"] - (0 - scaler.mean_[i]) / scaler.scale_[i] * ridge.coef_[i]
        for idx, row in now.iterrows():
            out.append({
                "season": int(season), "team_id": row["team_id"], "rating": float(ratings.loc[idx]),
                "baseline": float(row["prev_srs"]), "actual": None if pd.isna(row["srs"]) else float(row["srs"]),
                "contributions": {g: round(float(parts.loc[idx, cols].sum()), 2) for g, cols in cfg["groups"].items()},
                "features": {k: (None if pd.isna(row.get(k)) else round(float(row[k]), 4))
                             for k in features + ["n_transfers_in", "n_transfers_out"]},
            })
    return out


def write_preseason(conn, league, rows):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_preseason WHERE league = %s", (league,))
        cur.executemany(
            "INSERT INTO team_preseason (league, season, team_id, rating, baseline, actual, contributions, features) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(league, r["season"], r["team_id"], r["rating"], r["baseline"], r["actual"], Jsonb(r["contributions"]),
              Jsonb(r["features"])) for r in rows])


def run(conn, league):
    df = build_features(conn, league)
    labeled = df[df["srs"].notna()]
    cfg = CONFIG[league]
    print(f"[{league}] {len(df)} team-seasons ({len(labeled)} with a result)", flush=True)
    for name, (mae, r2, loso) in evaluate(league, df).items():
        print(f"  {name:26s} test {cfg['forward_split'] + 1}+: MAE {mae:5.2f} pts, R² {r2:.3f} | "
              f"leave-one-season-out {cfg['loso_from']}+: MAE {loso:5.2f}", flush=True)
    full = model().fit(prepare(labeled, features_for(league)), labeled["srs"])
    coefs = pd.Series(full[-1].coef_, index=features_for(league)).sort_values(key=abs, ascending=False)
    print("  standardized weights:", ", ".join(f"{k} {v:+.2f}" for k, v in coefs.head(10).items()), flush=True)
    rows = preseason_ratings(league, df)
    write_preseason(conn, league, rows)
    rated = pd.DataFrame(rows).dropna(subset=["actual"])
    recent = rated[rated["season"] > cfg["forward_split"]]
    print(f"  wrote {len(rows)} preseason ratings; seasons {cfg['forward_split'] + 1}+: corr with actual "
          f"{recent['rating'].corr(recent['actual']):.3f} vs last season {recent['baseline'].corr(recent['actual']):.3f}",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description="Offseason movement vs next-season strength; preseason ratings.")
    parser.add_argument("--league", nargs="+", choices=list(CONFIG), default=list(CONFIG))
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        for league in args.league:
            run(conn, league)


if __name__ == "__main__":
    main()
