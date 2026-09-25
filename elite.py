"""What makes an elite college season? Case studies, correlations and an elite-season model.

"Elite" = a top-10 FBS season by strength (SRS). Everything is measured on all FBS team-seasons
(2016-2025, the full seasons with talent data), so the dominant programs are compared with
teams that had similar resources and didn't win, not studied in isolation.

    python elite.py            # print the case studies, correlations and model evaluation
    python elite.py --write    # also write each team's elite-season probability for every season

Features known before a season (usable in the model):
    history       last season's SRS, two seasons back
    roster        recruiting (4-class average), talent composite, blue-chip ratio (share of
                  4/5-star signees in the last four classes)
    continuity    returning offense / defense production, head coach tenure, new coach
    QB            last season's leading passer returns; a transfer QB who led his old team
    portal        incoming production (level-weighted) and 247 ratings in / out, counts
On-field profile (what elite teams do during the season, descriptive only):
    EPA per play on offense / defense, success rates, turnover margin, third-down rate, havoc rate
"""

import argparse

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import offseason
import web_data
from database import connect, init_db

FIRST, LAST = 2016, 2025          # full seasons with talent data; 2026 is in progress
ELITE_RANK = 10
CASE_STUDIES = {"194": "Ohio State", "2483": "Oregon", "61": "Georgia", "84": "Indiana", "2390": "Miami",
                "87": "Notre Dame"}

PRESEASON = ["prev_srs", "prev2_srs", "recruiting", "talent", "blue_chip", "ret_off", "ret_def", "coach_tenure",
             "new_coach", "qb_returns", "qb_transfer", "in_off", "in_def", "transfer_in_rating",
             "transfer_out_rating", "n_transfers_in", "n_transfers_out"]
# The elite-season model: tested against richer feature sets (portal, returning production,
# QB flags, talent composite), none of which improved it on seasons it hadn't seen.
ELITE_FEATURES = ["prev_srs", "prev2_srs", "blue_chip", "coach_tenure", "new_coach"]
ON_FIELD = ["off_epa", "def_epa", "off_success", "def_success", "turnover_margin", "third_down_pct", "havoc_rate",
            "sack_rate_allowed"]
LABELS = {
    "prev_srs": "Last season's strength", "prev2_srs": "Strength two seasons ago",
    "recruiting": "Recruiting (4-class avg)", "talent": "Talent composite", "blue_chip": "Blue-chip ratio",
    "ret_off": "Returning offense", "ret_def": "Returning defense", "coach_tenure": "Head coach tenure (yrs)",
    "new_coach": "New head coach", "qb_returns": "Starting QB returns", "qb_transfer": "Transfer QB (led old team)",
    "in_off": "Portal offense added", "in_def": "Portal defense added", "transfer_in_rating": "Portal ratings in",
    "transfer_out_rating": "Portal ratings out", "n_transfers_in": "Transfers in", "n_transfers_out": "Transfers out",
    "off_epa": "Offense EPA/play", "def_epa": "Defense EPA/play allowed", "off_success": "Offense success rate",
    "def_success": "Defense success rate allowed", "turnover_margin": "Turnover margin / game",
    "third_down_pct": "Third-down conversion %", "havoc_rate": "Havoc plays / opponent play",
    "sack_rate_allowed": "Sack rate allowed",
}


def build(conn):
    df = offseason.build_features(conn, "cfb")
    df = df[df["season"].between(FIRST, LAST + 1)].copy()
    keys = list(zip(df["season"], df["team_id"]))

    # Blue-chip ratio: share of 4/5-star high-school signees across the last four classes.
    recruits = offseason.query_frame(conn, "SELECT season, team_id, stars FROM recruits WHERE league = 'cfb' "
                                           "AND team_id IS NOT NULL")
    by = recruits.assign(blue=(recruits["stars"] >= 4).astype(float)).groupby(["season", "team_id"])["blue"]
    counts, blues = by.size().to_dict(), by.sum().to_dict()
    df["blue_chip"] = [sum(blues.get((s - k, t), 0) for k in range(4)) / max(1, sum(counts.get((s - k, t), 0)
                       for k in range(4))) for s, t in keys]

    # Head coach tenure: consecutive seasons with this coach, including this one.
    coaches = {(s, t): c for s, t, c in conn.execute("SELECT season, team_id, coach FROM head_coaches "
                                                      "WHERE league = 'cfb'")}
    tenure = []
    for s, t in keys:
        n, coach = 0, coaches.get((s, t))
        while coach is not None and coaches.get((s - n, t)) == coach:
            n += 1
        tenure.append(n)
    df["coach_tenure"] = tenure

    # QB: last season's leading passer is back (ret_passing is his team's share, so >= 0.5 means
    # the main passer returned); a transfer who threw most of his old team's passes.
    df["qb_returns"] = (df["ret_passing"] >= 0.5).astype(float)
    df["qb_transfer"] = (df["in_level_passing"] >= 0.4).astype(float)

    # Season on-field profile (descriptive): efficiency from our play-by-play, box-score rates.
    epa = offseason.query_frame(conn, """
        SELECT g.season, s.team_id,
               sum((s.stats->>'off_epa')::float * (s.stats->>'off_plays')::float) / nullif(sum((s.stats->>'off_plays')::float), 0) off_epa,
               sum((s.stats->>'def_epa')::float * (s.stats->>'def_plays')::float) / nullif(sum((s.stats->>'def_plays')::float), 0) def_epa,
               sum((s.stats->>'off_success')::float * (s.stats->>'off_plays')::float) / nullif(sum((s.stats->>'off_plays')::float), 0) off_success,
               sum((s.stats->>'def_success')::float * (s.stats->>'def_plays')::float) / nullif(sum((s.stats->>'def_plays')::float), 0) def_success,
               sum((s.stats->>'off_sack_rate')::float * (s.stats->>'off_plays')::float) / nullif(sum((s.stats->>'off_plays')::float), 0) sack_rate_allowed,
               sum((s.stats->>'def_plays')::float) def_plays
        FROM team_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = 'cfb' AND s.source = 'espn_pbp' GROUP BY 1, 2""")
    df = df.merge(epa, on=["season", "team_id"], how="left")
    box = []
    for season in range(FIRST, LAST + 1):
        for team_id, t in web_data.team_seasons("cfb", season).items():
            box.append({"season": season, "team_id": team_id,
                        "turnover_margin": t["to_margin"] / t["games"] if t["to_margin"] is not None else None,
                        "third_down_pct": t["third_pct"]})
    df = df.merge(pd.DataFrame(box), on=["season", "team_id"], how="left")
    havoc = offseason.query_frame(conn, """
        SELECT g.season, s.team_id, sum(coalesce((s.stats->>'tfl')::float, 0) + coalesce((s.stats->>'def_int')::float, 0)
               + coalesce((s.stats->>'fumbles_rec')::float, 0)) havoc
        FROM team_game_stats s JOIN games g USING (league, game_id)
        WHERE s.league = 'cfb' AND s.source = 'box' GROUP BY 1, 2""")
    df = df.merge(havoc, on=["season", "team_id"], how="left")
    df["havoc_rate"] = df["havoc"] / df["def_plays"]

    # Outcomes: SRS rank among FBS, elite flag, record, final AP rank.
    df["srs_rank"] = df.groupby("season")["srs"].rank(ascending=False)
    df["elite"] = (df["srs_rank"] <= ELITE_RANK).astype(float)
    final_ap = offseason.query_frame(conn, """
        SELECT DISTINCT ON (season, team_id) season, team_id, rank AS final_ap FROM polls
        WHERE league = 'cfb' AND poll = 'AP Top 25' ORDER BY season, team_id, season_type DESC, week DESC""")
    last_week = offseason.query_frame(conn, """
        SELECT season, max(season_type * 100 + week) AS last FROM polls WHERE league = 'cfb' AND poll = 'AP Top 25'
        GROUP BY 1""")
    final_ap = offseason.query_frame(conn, """
        SELECT p.season, p.team_id, p.rank AS final_ap FROM polls p
        JOIN (SELECT season, max(season_type * 100 + week) AS last FROM polls WHERE league = 'cfb'
              AND poll = 'AP Top 25' GROUP BY 1) l ON l.season = p.season AND p.season_type * 100 + p.week = l.last
        WHERE p.league = 'cfb' AND p.poll = 'AP Top 25'""")
    del last_week
    df = df.merge(final_ap, on=["season", "team_id"], how="left")
    wins = []
    for season in range(FIRST, LAST + 1):
        for team_id, r in web_data.records("cfb", season).items():
            wins.append({"season": season, "team_id": team_id, "record": r["overall"], "win_pct": r["pct"]})
    return df.merge(pd.DataFrame(wins), on=["season", "team_id"], how="left")


def pct_rank(df, col):
    return df.groupby("season")[col].rank(pct=True)


def case_studies(df):
    full = df[df["season"] <= LAST]
    rows = full[full["team_id"].isin(CASE_STUDIES) & (full["season"] >= 2021)].copy()
    cols = ["talent", "blue_chip", "recruiting", "ret_off", "ret_def", "n_transfers_in", "in_off", "in_def",
            "coach_tenure", "off_epa", "def_epa"]
    for c in cols:
        rows[c + "_pct"] = [pct_rank(full, c).loc[i] for i in rows.index]
    print("\n== Case studies, 2021-2025 (percentile among FBS that season; 100 = best) ==")
    print(f"{'team':12s} {'yr':4s} {'record':6s} {'SRS#':>4s} {'AP':>3s} | {'talent':>6s} {'blue%':>5s} "
          f"{'ret O':>5s} {'ret D':>5s} {'xfer in':>7s} {'portal O':>8s} {'portal D':>8s} {'coach yrs':>9s} {'QB':>10s} | "
          f"{'off EPA':>7s} {'def EPA':>7s}")
    for r in rows.sort_values(["team_id", "season"]).itertuples():
        qb = "returning" if r.qb_returns else ("transfer" if r.qb_transfer else "new")
        print(f"{CASE_STUDIES[r.team_id]:12s} {r.season:4d} {r.record or '':6s} {int(r.srs_rank):4d} "
              f"{'' if pd.isna(r.final_ap) else int(r.final_ap):>3} | {r.talent_pct * 100:6.0f} {r.blue_chip:5.0%} "
              f"{r.ret_off_pct * 100:5.0f} {r.ret_def_pct * 100:5.0f} {r.n_transfers_in:7.0f} {r.in_off_pct * 100:8.0f} "
              f"{r.in_def_pct * 100:8.0f} {r.coach_tenure:9.0f} {qb:>10s} | {r.off_epa_pct * 100:7.0f} "
              f"{(1 - r.def_epa_pct) * 100 + 0:7.0f}")


def correlations(df):
    full = df[(df["season"] <= LAST) & df["srs"].notna()]
    print(f"\n== What separates elite seasons: all FBS team-seasons {FIRST}-{LAST} "
          f"({len(full)} seasons, {int(full['elite'].sum())} elite) ==")
    # Among teams with top-25 talent too: what separates the elite from the rest of the blue bloods?
    rich = full[pct_rank(full, "talent").loc[full.index] >= 1 - 25 / 130]
    print(f"{'':30s} {'corr w/ SRS':>11s} {'elite avg':>9s} {'others':>7s} | "
          f"{'top-25-talent teams: elite':>26s} {'not elite':>9s}")
    for group, cols in (("Known before the season", PRESEASON), ("During the season", ON_FIELD)):
        print(f"  {group}")
        out = []
        for c in cols:
            x = full[c].astype(float)
            corr = x.corr(full["srs"], method="spearman")
            out.append((abs(corr), c, corr, x[full["elite"] == 1].mean(), x[full["elite"] == 0].mean(),
                        rich.loc[rich["elite"] == 1, c].mean(), rich.loc[rich["elite"] == 0, c].mean()))
        for _, c, corr, e, o, re_, rn in sorted(out, reverse=True):
            print(f"    {LABELS[c]:28s} {corr:+11.2f} {e:9.3f} {o:7.3f} | {re_:26.3f} {rn:9.3f}")
    print(f"  (top-25-talent seasons: {len(rich)}, of which elite: {int(rich['elite'].sum())})")


def model():
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))


def prepare(frame, cols, means):
    return frame[cols].astype(float).fillna(means).fillna(0.0)


def evaluate(df):
    full = df[(df["season"] <= LAST) & df["srs"].notna()]
    print("\n== Elite-season model: P(top-10 season) from preseason information only ==")
    print("   each season predicted by a model fit only on earlier seasons; tested on 2020-2025")
    for name, cols in (("last season only", ["prev_srs"]), ("last 2 seasons + roster talent",
                                                              ["prev_srs", "prev2_srs", "recruiting", "talent", "blue_chip"]),
                       ("full preseason profile", PRESEASON),
                       ("ELITE MODEL: history + blue-chip + coach", ELITE_FEATURES)):
        probs, ys = [], []
        for season in range(2020, LAST + 1):
            train, test = full[full["season"] < season], full[full["season"] == season]
            means = train[cols].astype(float).mean()
            m = model().fit(prepare(train, cols, means), train["elite"])
            probs.append(m.predict_proba(prepare(test, cols, means))[:, 1])
            ys.append(test["elite"].to_numpy())
        p, y = np.concatenate(probs), np.concatenate(ys)
        # Of each season's 10 most likely teams, how many had elite seasons?
        hits = sum(int(yy[np.argsort(-pp)[:10]].sum()) for pp, yy in zip(probs, ys))
        print(f"   {name:42s} AUC {roc_auc_score(y, p):.3f}  log loss {log_loss(y, p):.3f}  "
              f"Brier {brier_score_loss(y, p):.4f}  top-10 picks that were elite: {hits}/{10 * len(probs)}")
    means = full[ELITE_FEATURES].astype(float).mean()
    m = model().fit(prepare(full, ELITE_FEATURES, means), full["elite"])
    coefs = pd.Series(m[-1].coef_[0], index=ELITE_FEATURES).sort_values(key=abs, ascending=False)
    print("   what the elite model weighs (standardized; + raises the odds of an elite season):")
    for c, v in coefs.items():
        print(f"     {LABELS[c]:30s} {v:+.2f}")
    return m, means


def elite_probabilities(df):
    """Forward-only P(elite) for every season 2020+ (incl. the current one)."""
    out = []
    labeled = df[df["srs"].notna() & (df["season"] <= LAST)]
    for season in sorted(df["season"].unique()):
        if season < 2020:
            continue
        train = labeled[labeled["season"] < season]
        now = df[df["season"] == season]
        means = train[ELITE_FEATURES].astype(float).mean()
        p = model().fit(prepare(train, ELITE_FEATURES, means), train["elite"]).predict_proba(
            prepare(now, ELITE_FEATURES, means))[:, 1]
        out += [(int(season), t, float(v)) for t, v in zip(now["team_id"], p)]
    return out


def main():
    parser = argparse.ArgumentParser(description="What makes an elite college season.")
    parser.add_argument("--write", action="store_true", help="store elite-season probabilities in team_preseason")
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        df = build(conn)
        case_studies(df)
        correlations(df)
        evaluate(df)
        if args.write:
            rows = elite_probabilities(df)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany("UPDATE team_preseason SET features = features || %s WHERE league = 'cfb' "
                                "AND season = %s AND team_id = %s",
                                [(Jsonb({"elite_prob": round(p, 4)}), s, t) for s, t, p in rows])
            print(f"\nwrote elite-season probabilities for {len(rows)} team-seasons", flush=True)


if __name__ == "__main__":
    main()
