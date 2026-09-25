"""How offseason player movement relates to a team's next season (college first).

For each team-season this builds:
    target      season strength: SRS (points better than an average team, neutral field), fit on
                FBS-vs-FBS games of that season
    prev_srs    last season's SRS
    movement    per production category (passing, rushing, receiving, tackles, havoc = TFL + sacks
                + INT + passes defended), as shares of team production:
                  returning  last season's share from players back on the roster
                  incoming   transfers' share of their old team's production
                  incoming_level  the same, weighted by the old team's strength vs the new team's
                plus 247 ratings of transfers in/out, recruiting, talent and a new-head-coach flag

A ridge regression maps these to the season's SRS. It's evaluated two ways, never on seasons
it was fit on: leave-one-season-out over the portal era, and forward (fit through 2022, test
2023+) against a baseline of last season's SRS pulled toward the mean.

    python offseason.py                  # evaluate, then write team_preseason ratings (each season
                                         # fit only on earlier seasons) with contribution breakdowns
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
FCS_SRS = -25.0         # strength assumed for a non-FBS origin team
SRS_SHRINK = 2.0        # ridge penalty (in games) when fitting season SRS
FIRST_SEASON = 2006     # needs the previous season's production
PORTAL_FIRST = 2021


def query_frame(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def season_srs(conn):
    """{(season, team_id): SRS} from completed FBS-vs-FBS games (regular season and bowls)."""
    games = query_frame(conn, """
        SELECT g.season, g.home_team_id, g.away_team_id, g.home_score - g.away_score AS margin, g.neutral_site
        FROM games g
        JOIN team_affiliations h ON h.league = g.league AND h.season = g.season AND h.team_id = g.home_team_id
        JOIN team_affiliations a ON a.league = g.league AND a.season = g.season AND a.team_id = g.away_team_id
        WHERE g.league = 'cfb' AND g.completed AND h.classification = 'fbs' AND a.classification = 'fbs'
    """)
    out = {}
    for season, g in games.groupby("season"):
        teams = sorted(set(g["home_team_id"]) | set(g["away_team_id"]))
        idx = {t: i for i, t in enumerate(teams)}
        X = np.zeros((len(g), len(teams) + 1))
        rows = np.arange(len(g))
        X[rows, g["home_team_id"].map(idx)] = 1
        X[rows, g["away_team_id"].map(idx)] = -1
        X[:, -1] = np.where(g["neutral_site"].fillna(False).astype(bool), 0, 1)  # home field
        penalty = np.full(len(teams) + 1, SRS_SHRINK)
        penalty[-1] = 1e-6
        # Margins are capped so blowouts of bad teams don't dominate.
        y = g["margin"].clip(-35, 35).to_numpy(float)
        beta = np.linalg.solve(X.T @ X + np.diag(penalty), X.T @ y)
        for t, i in idx.items():
            out[(season, t)] = beta[i]
    return out


def season_off_def(conn):
    """{(season, team_id): (offense, defense)}: points scored above average and points allowed
    below average vs an average FBS opponent (so offense + defense ~ SRS)."""
    games = query_frame(conn, """
        SELECT g.season, g.home_team_id, g.away_team_id, g.home_score, g.away_score, g.neutral_site
        FROM games g
        JOIN team_affiliations h ON h.league = g.league AND h.season = g.season AND h.team_id = g.home_team_id
        JOIN team_affiliations a ON a.league = g.league AND a.season = g.season AND a.team_id = g.away_team_id
        WHERE g.league = 'cfb' AND g.completed AND h.classification = 'fbs' AND a.classification = 'fbs'
    """)
    out = {}
    for season, g in games.groupby("season"):
        teams = sorted(set(g["home_team_id"]) | set(g["away_team_id"]))
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        # Two rows per game: each side's points = mean + home edge + offense[scorer] - defense[opponent]
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


def player_shares(conn):
    """Per (season, team, player): share of the team's season production in each category."""
    rows = conn.execute("""
        SELECT g.season, s.team_id, s.player_id, s.stats FROM player_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = 'cfb' AND s.source = 'box'
    """).fetchall()
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


def build_features(conn):
    srs = season_srs(conn)
    srs_frame = pd.DataFrame([(s, t, v) for (s, t), v in srs.items()], columns=["season", "team", "srs"])
    shares = player_shares(conn)
    cats = list(CATEGORIES)
    roster = query_frame(conn, "SELECT DISTINCT season, team_id AS team, player_id AS player FROM rosters "
                               "WHERE league = 'cfb'")
    teams = query_frame(conn, "SELECT season, team_id AS team FROM team_affiliations "
                              "WHERE league = 'cfb' AND classification = 'fbs' AND season >= %s", (FIRST_SEASON,))
    fbs_prev = set(map(tuple, teams[["season", "team"]].to_numpy()))

    df = teams.merge(srs_frame, on=["season", "team"], how="left")
    prev = srs_frame.assign(season=srs_frame["season"] + 1).rename(columns={"srs": "prev_srs"})
    df = df.merge(prev, on=["season", "team"], how="left")
    df["prev_srs"] = df["prev_srs"].fillna(FCS_SRS)  # new to FBS

    # Returning: last season's shares of players on this season's roster for the same team.
    last = shares.assign(season=shares["season"] + 1)
    back = last.merge(roster, on=["season", "team", "player"]).groupby(["season", "team"])[cats].sum()
    df = df.merge(back.add_prefix("ret_").reset_index(), on=["season", "team"], how="left")

    # Incoming transfers: their share of their old team's production, and that share weighted by
    # how strong the old team was relative to the new one.
    transfers = query_frame(conn, "SELECT season, player_id AS player, origin_team_id AS origin, dest_team_id AS team, "
                                  "rating FROM transfers WHERE league = 'cfb'")
    moved = transfers.dropna(subset=["player", "team"]).merge(
        last.drop(columns="team").groupby(["season", "player"], as_index=False)[cats].sum(), on=["season", "player"])
    origin_srs = srs_frame.assign(season=srs_frame["season"] + 1).rename(columns={"team": "origin", "srs": "origin_srs"})
    moved = moved.merge(origin_srs, on=["season", "origin"], how="left").merge(
        df[["season", "team", "prev_srs"]], on=["season", "team"], how="left")
    level = np.clip(1 + (moved["origin_srs"].fillna(FCS_SRS) - moved["prev_srs"].fillna(0)) / 30, 0.2, 1.5)
    incoming = moved.groupby(["season", "team"])[cats].sum().add_prefix("in_")
    incoming_level = moved[cats].mul(level, axis=0).assign(season=moved["season"], team=moved["team"]) \
        .groupby(["season", "team"])[cats].sum().add_prefix("in_level_")
    df = df.merge(incoming.reset_index(), on=["season", "team"], how="left")
    df = df.merge(incoming_level.reset_index(), on=["season", "team"], how="left")

    # 247 transfer ratings (unrated transfers count as a 0.80 baseline), in and out.
    t = transfers.assign(value=transfers["rating"].fillna(0.80) - 0.80)
    df = df.merge(t.dropna(subset=["team"]).groupby(["season", "team"]).agg(
        transfer_in_rating=("value", "sum"), n_transfers_in=("value", "size")).reset_index(), on=["season", "team"], how="left")
    df = df.merge(t.rename(columns={"team": "dest", "origin": "team"}).dropna(subset=["team"]).groupby(["season", "team"]).agg(
        transfer_out_rating=("value", "sum"), n_transfers_out=("value", "size")).reset_index(), on=["season", "team"], how="left")

    movement = [c for c in df.columns if c.startswith(("ret_", "in_", "transfer_", "n_transfers"))]
    df[movement] = df[movement].fillna(0.0)

    # Offense / defense split: returning and incoming production scale each side of last season.
    od = season_off_def(conn)
    df["prev_off"] = [od.get((s - 1, t), (FCS_SRS / 2, FCS_SRS / 2))[0] for s, t in zip(df["season"], df["team"])]
    df["prev_def"] = [od.get((s - 1, t), (FCS_SRS / 2, FCS_SRS / 2))[1] for s, t in zip(df["season"], df["team"])]
    # Passing counts double on offense (the QB); havoc double on defense.
    df["ret_off"] = (2 * df["ret_passing"] + df["ret_rush"] + df["ret_rec"]) / 4
    df["ret_def"] = (df["ret_tackles"] + 2 * df["ret_havoc"]) / 3
    df["in_off"] = (2 * df["in_level_passing"] + df["in_level_rush"] + df["in_level_rec"]) / 4
    df["in_def"] = (df["in_level_tackles"] + 2 * df["in_level_havoc"]) / 3
    # Program level: two seasons back, and last season's efficiency (steadier than points margin).
    df["prev2_srs"] = [srs.get((s - 2, t), FCS_SRS) for s, t in zip(df["season"], df["team"])]
    epa = query_frame(conn, """
        SELECT g.season + 1 AS season, s.team_id AS team,
               sum((s.stats->>'off_epa')::float * (s.stats->>'off_plays')::float) / nullif(sum((s.stats->>'off_plays')::float), 0) AS prev_off_epa,
               sum((s.stats->>'def_epa')::float * (s.stats->>'def_plays')::float) / nullif(sum((s.stats->>'def_plays')::float), 0) AS prev_def_epa
        FROM team_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = 'cfb' AND s.source = 'espn_pbp' GROUP BY 1, 2""")
    df = df.merge(epa, on=["season", "team"], how="left")
    df["prev_off_x_ret"] = df["prev_off"] * df["ret_off"]
    df["prev_def_x_ret"] = df["prev_def"] * df["ret_def"]

    context = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT season, team_id, stats FROM team_seasons WHERE league = 'cfb' AND source = 'cfbd'")}
    df["talent"] = [(context.get((s, t)) or {}).get("talent") for s, t in zip(df["season"], df["team"])]
    df["recruiting"] = [np.nanmean([(context.get((s - k, t)) or {}).get("recruiting_points", np.nan) for k in range(4)])
                        if any((context.get((s - k, t)) or {}).get("recruiting_points") is not None for k in range(4))
                        else np.nan for s, t in zip(df["season"], df["team"])]
    coaches = {(s, t): c for s, t, c in conn.execute("SELECT season, team_id, coach FROM head_coaches WHERE league = 'cfb'")}
    df["new_coach"] = [float(coaches.get((s, t)) is not None and coaches.get((s - 1, t)) is not None
                             and coaches[(s, t)] != coaches[(s - 1, t)]) for s, t in zip(df["season"], df["team"])]
    del fbs_prev
    return df.rename(columns={"team": "team_id"})


BASELINE = ["prev_srs"]
CONTEXT = ["prev_srs", "prev2_srs", "prev_off_epa", "prev_def_epa", "recruiting", "talent", "new_coach"]
# Returning production scales how much of last season's offense/defense carries over; incoming
# transfer production (weighted by the old team's level) and 247 ratings add on top.
FEATURES = CONTEXT + ["prev_off", "prev_def", "prev_off_x_ret", "prev_def_x_ret", "ret_off", "ret_def",
                      "in_off", "in_def", "transfer_in_rating", "transfer_out_rating"]


def model(alpha=10.0):
    return make_pipeline(StandardScaler(), Ridge(alpha=alpha))


def prepare(df, cols):
    """Fill gaps with the column mean (0 if a column has no data yet, e.g. talent before 2015)."""
    X = df[cols].astype(float)
    return X.fillna(X.mean()).fillna(0.0)


def evaluate(df):
    labeled = df[df["srs"].notna()]
    results = {}
    for name, cols in (("baseline: last season", BASELINE), ("+ history/recruiting/coach", CONTEXT),
                       ("+ offseason movement", FEATURES)):
        # Forward: fit through 2022, test 2023+
        train, test = labeled[labeled["season"] <= 2022], labeled[labeled["season"] >= 2023]
        m = model().fit(prepare(train, cols), train["srs"])
        pred = m.predict(prepare(test, cols).fillna(0))
        fwd = (np.mean(np.abs(pred - test["srs"])), np.corrcoef(pred, test["srs"])[0, 1] ** 2)
        # Leave one portal-era season out
        errs = []
        for season in range(PORTAL_FIRST, int(labeled["season"].max()) + 1):
            tr, te = labeled[labeled["season"] != season], labeled[labeled["season"] == season]
            if te.empty:
                continue
            p = model().fit(prepare(tr, cols), tr["srs"]).predict(prepare(te, cols))
            errs.append(np.abs(p - te["srs"]))
        loso = float(np.mean(np.concatenate(errs)))
        results[name] = (fwd[0], fwd[1], loso)
    return results


GROUPS = {
    "history": ["prev_srs", "prev2_srs", "prev_off_epa", "prev_def_epa", "prev_off", "prev_def"],
    "recruiting": ["recruiting", "talent"],
    "returning": ["ret_off", "ret_def", "prev_off_x_ret", "prev_def_x_ret"],
    "transfers": ["in_off", "in_def", "transfer_in_rating", "transfer_out_rating"],
    "coaching": ["new_coach"],
}
FIRST_RATED = 2009  # needs a few seasons to fit on


def preseason_ratings(df):
    """For each season, fit on earlier seasons only and rate every team before it plays.
    Contributions: each group's share of the rating vs an average team (points)."""
    out = []
    for season in sorted(df["season"].unique()):
        if season < FIRST_RATED:
            continue
        train = df[(df["season"] < season) & df["srs"].notna()]
        now = df[df["season"] == season]
        X_train = prepare(train, FEATURES)
        m = model().fit(X_train, train["srs"])
        X_now = now[FEATURES].astype(float).fillna(X_train.mean()).fillna(0.0)
        scaler, ridge = m[0], m[-1]
        z = (X_now - scaler.mean_) / scaler.scale_
        parts = z * ridge.coef_
        ratings = ridge.intercept_ + parts.sum(axis=1)
        # Display only: show coaching relative to keeping the same coach (0), not to the average team.
        i = FEATURES.index("new_coach")
        parts["new_coach"] = parts["new_coach"] - (0 - scaler.mean_[i]) / scaler.scale_[i] * ridge.coef_[i]
        for i, (idx, row) in enumerate(now.iterrows()):
            contrib = {g: round(float(parts.loc[idx, cols].sum()), 2) for g, cols in GROUPS.items()}
            out.append({"season": int(season), "team_id": row["team_id"], "rating": float(ratings.loc[idx]),
                        "baseline": float(row["prev_srs"]), "actual": None if pd.isna(row["srs"]) else float(row["srs"]),
                        "contributions": contrib,
                        "features": {k: (None if pd.isna(row[k]) else round(float(row[k]), 4))
                                     for k in FEATURES + ["n_transfers_in", "n_transfers_out"]}})
    return out


def write_preseason(conn, rows):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_preseason WHERE league = 'cfb'")
        cur.executemany(
            "INSERT INTO team_preseason (league, season, team_id, rating, baseline, actual, contributions, features) "
            "VALUES ('cfb', %s, %s, %s, %s, %s, %s, %s)",
            [(r["season"], r["team_id"], r["rating"], r["baseline"], r["actual"], Jsonb(r["contributions"]),
              Jsonb(r["features"])) for r in rows])


def main():
    argparse.ArgumentParser(description="Offseason movement vs next-season strength (CFB).").parse_args()
    with connect() as conn:
        init_db(conn)
        df = build_features(conn)
    labeled = df[df["srs"].notna()]
    print(f"{len(df)} team-seasons ({len(labeled)} with a result), {int((df['in_passing'] > 0).sum())} with incoming "
          f"passing production", flush=True)
    for name, (mae, r2, loso) in evaluate(df).items():
        print(f"  {name:28s} test 2023+: MAE {mae:5.2f} pts, R² {r2:.3f} | leave-one-season-out 2021+: MAE {loso:5.2f}")
    full = model().fit(prepare(labeled, FEATURES), labeled["srs"])
    coefs = pd.Series(full[-1].coef_, index=FEATURES).sort_values(key=abs, ascending=False)
    print("  standardized weights:", ", ".join(f"{k} {v:+.2f}" for k, v in coefs.head(12).items()))
    rows = preseason_ratings(df)
    with connect() as conn:
        write_preseason(conn, rows)
    print(f"  wrote {len(rows)} preseason ratings ({FIRST_RATED}+), each fit only on earlier seasons", flush=True)


if __name__ == "__main__":
    main()
