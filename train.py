"""Train XGBoost game models on game_features and compare them with Elo and the closing line.

Three variants per league:
    xgb         no betting-line inputs: can the model find signal the market doesn't have?
    xgb_market  starts from the closing line and learns corrections to it (games with a line only)
    linear      logistic/ridge regression on a few strong features; on NFL-sized data this
                beats the full XGBoost feature set, which overfits

Each variant has three models (home win probability, margin, total). They are trained on
TRAIN seasons with early stopping on VALIDATION, and scored on the test seasons after it.
Out-of-sample predictions (validation season onward) are written to predictions; upcoming
games are predicted by a refit on every completed game.

    python train.py
    python train.py --league nfl
"""

import argparse
import re
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from psycopg.types.json import Jsonb
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from database import connect, init_db
from espn_client import LEAGUES

TRAIN_START = 2006     # 2005 rows have no form history
TRAIN_END = 2021
VALIDATION = 2022      # test = everything after
MODEL_DIR = Path(__file__).parent / "models"

LINE_FEATURES = ["line_spread", "line_total", "line_open_spread", "line_home_prob"]
# Kept in game_features for display, but they added no signal in testing or made XGBoost
# overfit: the supporting-cast ridge ratings (pass protection, YAC, run game), and the
# per-side / per-unit injury-report detail.
MODEL_EXCLUDE = re.compile(r"ridge_(off|def|net)_(pass_epa|rush_epa|sack_rate|yac_epa|rush_success)$"
                           # injury-report detail: models get only the four summary differences
                           r"|^(home|away)_(starters_out_off|starters_out_def|ol_out|skill_out|front_out|db_out"
                           r"|starters_questionable|missing_off_prod|missing_def_prod)$"
                           r"|^diff_(ol_out|skill_out|front_out|db_out|starters_questionable)$")
BASE_PARAMS = {
    "n_estimators": 3000, "learning_rate": 0.02, "max_depth": 3, "min_child_weight": 10,
    "subsample": 0.8, "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping_rounds": 150,
    "n_jobs": -1, "random_state": 0,
}
# Compact inputs for the linear variant (missing ones, e.g. EPA for CFB, are skipped).
LINEAR_FEATURES = {
    "win": ["elo_margin", "diff_ewm_margin", "diff_ridge_net_epa", "diff_ridge_net_success",
            "diff_qb_rating", "diff_qb_vs_prev", "diff_qb_changed", "diff_rest_days", "neutral_site",
            # CFB preseason context (absent for the NFL, so skipped there)
            "diff_prev_sp_rating", "diff_recruiting_avg", "diff_talent", "diff_returning_ppa_pct",
            "diff_preseason_rating",
            # NFL availability: starters and production ruled out on the injury report
            "diff_starters_out_off", "diff_starters_out_def", "diff_missing_off_prod", "diff_missing_def_prod"],
    "total": ["home_ewm_points_for", "away_ewm_points_for", "home_ewm_points_against",
              "away_ewm_points_against", "dome", "wind", "temp"],
}
LINEAR_FEATURES["margin"] = LINEAR_FEATURES["win"]

TARGETS = {
    # name: (estimator, objective/metric, target column)
    "win": (xgb.XGBClassifier, {"objective": "binary:logistic", "eval_metric": "logloss"}, "home_win"),
    "margin": (xgb.XGBRegressor, {"objective": "reg:squarederror", "eval_metric": "mae"}, "margin"),
    "total": (xgb.XGBRegressor, {"objective": "reg:squarederror", "eval_metric": "mae"}, "total_points"),
}


def load(conn, league):
    cur = conn.execute(
        "SELECT game_id, season, start_time, completed, margin, total_points, features "
        "FROM game_features WHERE league = %s ORDER BY start_time, game_id",
        (league,),
    )
    rows = cur.fetchall()
    meta = pd.DataFrame([r[:6] for r in rows], columns=["game_id", "season", "start_time", "completed", "margin", "total_points"])
    feats = pd.DataFrame([r[6] for r in rows]).astype(float)
    df = pd.concat([meta, feats], axis=1)
    df["home_win"] = np.where(df["margin"] > 0, 1.0, np.where(df["margin"] < 0, 0.0, np.nan))
    return df, list(feats.columns)


# xgb_market starts every prediction from the closing line (XGBoost's base_margin) and only
# learns corrections to it, so it trains and predicts on games that have that line.
MARKET_BASE = {
    "win": ("line_home_prob", lambda p: np.log(p / (1 - p))),  # log-odds, the classifier's raw scale
    "margin": ("line_spread", lambda spread: -spread),
    "total": ("line_total", lambda total: total),
}


def base_margin(variant, kind, rows):
    if variant != "xgb_market":
        return None
    column, transform = MARKET_BASE[kind]
    return transform(rows[column].clip(0.001, 0.999) if kind == "win" else rows[column]).to_numpy()


def usable(variant, kind, rows):
    """Rows a variant can predict: market models need their line."""
    return rows if variant != "xgb_market" else rows[rows[MARKET_BASE[kind][0]].notna()]


def fit(kind, X, y, base=None, X_val=None, y_val=None, base_val=None, n_estimators=None):
    estimator, extra, _ = TARGETS[kind]
    params = {**BASE_PARAMS, **extra}
    if n_estimators is not None:  # refit with a fixed tree count, no early stopping
        params.update(n_estimators=n_estimators, early_stopping_rounds=None)
        return estimator(**params).fit(X, y, base_margin=base, verbose=False)
    return estimator(**params).fit(
        X, y, base_margin=base, eval_set=[(X_val, y_val)],
        base_margin_eval_set=None if base_val is None else [base_val], verbose=False,
    )


def predict(model, kind, X, base=None):
    extra = {} if base is None else {"base_margin": base}
    if kind == "win":
        return model.predict_proba(X, **extra)[:, 1]
    return model.predict(X, **extra)


def linear_model(kind):
    estimator = LogisticRegression(C=1.0, max_iter=1000) if kind == "win" else Ridge(alpha=1.0)
    return make_pipeline(SimpleImputer(), StandardScaler(), estimator)


def win_metrics(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {
        "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        "brier": float(np.mean((p - y) ** 2)),
        "accuracy": float(np.mean((p > 0.5) == (y == 1))),
    }


def report(league, name, df, preds):
    """Compare models with Elo and Vegas on identical game subsets."""
    played = df[df["margin"].notna()]
    decided = played[played["home_win"].notna()]
    print(f"[{league}] {name}: {len(played)} games")

    def row(label, p=None, margin=None, total=None, subset=None):
        idx = subset if subset is not None else played.index
        for series in (p, margin, total):
            if series is not None:
                idx = idx.intersection(series.index[series.notna()])
        out = []
        if p is not None:
            d = decided.index.intersection(idx)
            m = win_metrics(p.loc[d].to_numpy(), decided.loc[d, "home_win"].to_numpy())
            out.append(f"log_loss={m['log_loss']:.4f} brier={m['brier']:.4f} acc={m['accuracy']:.3f}")
        if margin is not None:
            out.append(f"margin_mae={np.mean(np.abs(played.loc[idx, 'margin'] - margin.loc[idx])):.2f}")
        if total is not None:
            out.append(f"total_mae={np.mean(np.abs(played.loc[idx, 'total_points'] - total.loc[idx])):.2f}")
        print(f"    {label:<22}{'  '.join(out)}  (n={len(idx)})")

    row("elo", played["elo_prob"], played["elo_margin"])
    for variant, p in preds.items():
        row(variant, p["win"], p["margin"], p["total"])

    # Same games as the market, where the market has a complete line.
    has_line = played.index[played[["line_spread", "line_total", "line_home_prob"]].notna().all(axis=1)]
    if len(has_line) < 50:
        print("    (too few games with a closing line to compare with Vegas)\n", flush=True)
        return
    print(f"  on the {len(has_line)} games with a closing line:")
    row("elo", played["elo_prob"], played["elo_margin"], subset=has_line)
    for variant, p in preds.items():
        row(variant, p["win"], p["margin"], p["total"], subset=has_line)
    row("vegas", played["line_home_prob"], -played["line_spread"], played["line_total"], subset=has_line)
    for variant, p in preds.items():
        edge = p["margin"].loc[has_line] + played.loc[has_line, "line_spread"]
        cover = played.loc[has_line, "margin"] + played.loc[has_line, "line_spread"]
        keep = cover != 0
        ats = np.mean((edge[keep] > 0) == (cover[keep] > 0))
        print(f"    {variant} vs spread (ATS) {ats:.3f}  (52.4% needed to beat -110 juice)")
    print(flush=True)


def run_league(conn, league):
    df, all_features = load(conn, league)
    model_features = [c for c in all_features if not MODEL_EXCLUDE.search(c)]
    variants = {
        "xgb": [c for c in model_features if c not in LINE_FEATURES],
        "xgb_market": model_features,
    }
    train = df[df["season"].between(TRAIN_START, TRAIN_END)]
    val = df[df["season"] == VALIDATION]
    test = df[df["season"] > VALIDATION]
    done = df[df["completed"] & df["season"].ge(TRAIN_START)]
    oos = df[df["season"] >= VALIDATION]  # rows that get out-of-sample predictions
    upcoming = oos[~oos["completed"]]
    preds_val, preds_test, stored = {}, {}, {}
    MODEL_DIR.mkdir(exist_ok=True)
    for variant, cols in variants.items():
        preds_val[variant], preds_test[variant], stored[variant] = {}, {}, {}
        for kind, (_, _, target) in TARGETS.items():
            def rows(frame, labeled=True):
                frame = usable(variant, kind, frame)
                return frame[frame[target].notna()] if labeled else frame

            def run(model, frame):
                return pd.Series(predict(model, kind, frame[cols], base_margin(variant, kind, frame)), index=frame.index)

            tr, va = rows(train), rows(val)
            model = fit(kind, tr[cols], tr[target], base_margin(variant, kind, tr),
                        va[cols], va[target], base_margin(variant, kind, va))
            trees = model.best_iteration + 1
            preds_val[variant][kind] = run(model, rows(val, labeled=False)).reindex(val.index)
            preds_test[variant][kind] = run(model, rows(test, labeled=False)).reindex(test.index)
            out = run(model, rows(oos, labeled=False)).reindex(oos.index)

            # Refit on every completed game for upcoming games, with the tree count found above.
            everything = rows(done)
            final = fit(kind, everything[cols], everything[target], base_margin(variant, kind, everything),
                        n_estimators=trees)
            ahead = rows(upcoming, labeled=False)
            if len(ahead):
                out.loc[ahead.index] = run(final, ahead)
            stored[variant][kind] = out
            joblib.dump({"model": final, "features": cols, "market_base": variant == "xgb_market"},
                        MODEL_DIR / f"{league}_{variant}_{kind}.joblib")

            if kind == "margin":
                importance = pd.Series(model.feature_importances_, index=cols).sort_values(ascending=False)
                top = ", ".join(f"{c} {v:.2f}" for c, v in importance.head(8).items())
                print(f"[{league}] {variant} margin model: {trees} trees, trained on {len(tr)} games; "
                      f"top features: {top}", flush=True)

    # Linear variant: fixed hyperparameters, so no early stopping; same train seasons as XGBoost.
    preds_val["linear"], preds_test["linear"], stored["linear"] = {}, {}, {}
    for kind, (_, _, target) in TARGETS.items():
        cols = [c for c in LINEAR_FEATURES[kind] if c in df and df[c].notna().any()]
        tr = train[train[target].notna()]
        model = linear_model(kind).fit(tr[cols], tr[target])
        preds_val["linear"][kind] = pd.Series(predict(model, kind, val[cols]), index=val.index)
        preds_test["linear"][kind] = pd.Series(predict(model, kind, test[cols]), index=test.index)
        out = pd.Series(predict(model, kind, oos[cols]), index=oos.index)
        everything = done[done[target].notna()]
        final = linear_model(kind).fit(everything[cols], everything[target])
        if len(upcoming):
            out.loc[upcoming.index] = predict(final, kind, upcoming[cols])
        stored["linear"][kind] = out
        joblib.dump({"model": final, "features": cols}, MODEL_DIR / f"{league}_linear_{kind}.joblib")
    print(flush=True)

    report(league, f"validation {VALIDATION} (used for early stopping)", val, preds_val)
    report(league, f"test {VALIDATION + 1}-{int(df['season'].max())}", test, preds_test)
    write_predictions(conn, league, oos, stored)


def write_predictions(conn, league, oos, stored):
    rows = []
    for variant, preds in stored.items():
        for i, game_id in oos["game_id"].items():
            values = [None if pd.isna(preds[kind][i]) else float(preds[kind][i]) for kind in ("win", "margin", "total")]
            if all(v is None for v in values):
                continue
            rows.append((league, game_id, variant, *values,
                         Jsonb({"out_of_sample": bool(oos.at[i, "completed"]), "train_end": TRAIN_END})))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM predictions WHERE league = %s AND model = ANY(%s)", (league, list(stored)))
        cur.executemany(
            "INSERT INTO predictions (league, game_id, model, home_win_prob, predicted_margin, predicted_total, details) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    print(f"[{league}] wrote {len(rows)} predictions ({', '.join(stored)})\n", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Train and evaluate XGBoost game models.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            run_league(conn, league)


if __name__ == "__main__":
    main()
