"""Train XGBoost game models on game_features and compare them with Elo and the closing line.

Two variants per league:
    xgb         no betting-line inputs: can the model find signal the market doesn't have?
    xgb_market  closing line as inputs too: the best-calibrated number for the site

Each variant has three models (home win probability, margin, total). They are trained on
TRAIN seasons with early stopping on VALIDATION, and scored on the test seasons after it.
Out-of-sample predictions (validation season onward) are written to predictions; upcoming
games are predicted by a refit on every completed game.

    python train.py
    python train.py --league nfl
"""

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from psycopg.types.json import Jsonb

from database import connect, init_db
from espn_client import LEAGUES

TRAIN_START = 2006     # 2005 rows have no form history
TRAIN_END = 2021
VALIDATION = 2022      # test = everything after
MODEL_DIR = Path(__file__).parent / "models"

LINE_FEATURES = ["line_spread", "line_total", "line_open_spread", "line_home_prob"]
BASE_PARAMS = {
    "n_estimators": 3000, "learning_rate": 0.02, "max_depth": 3, "min_child_weight": 10,
    "subsample": 0.8, "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping_rounds": 150,
    "n_jobs": -1, "random_state": 0,
}
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


def fit(kind, X, y, X_val, y_val, n_estimators=None):
    estimator, extra, _ = TARGETS[kind]
    params = {**BASE_PARAMS, **extra}
    if n_estimators is not None:  # refit with a fixed tree count, no early stopping
        params.update(n_estimators=n_estimators, early_stopping_rounds=None)
        return estimator(**params).fit(X, y, verbose=False)
    return estimator(**params).fit(X, y, eval_set=[(X_val, y_val)], verbose=False)


def predict(model, kind, X):
    return model.predict_proba(X)[:, 1] if kind == "win" else model.predict(X)


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
        out = []
        if p is not None:
            d = decided.index.intersection(idx)
            m = win_metrics(p.loc[d].to_numpy(), decided.loc[d, "home_win"].to_numpy())
            out.append(f"log_loss={m['log_loss']:.4f} brier={m['brier']:.4f} acc={m['accuracy']:.3f}")
        if margin is not None:
            out.append(f"margin_mae={np.mean(np.abs(played.loc[idx, 'margin'] - margin.loc[idx])):.2f}")
        if total is not None:
            out.append(f"total_mae={np.mean(np.abs(played.loc[idx, 'total_points'] - total.loc[idx])):.2f}")
        print(f"    {label:<22}{'  '.join(out)}")

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
    variants = {
        "xgb": [c for c in all_features if c not in LINE_FEATURES],
        "xgb_market": all_features,
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
            tr, va = train[train[target].notna()], val[val[target].notna()]
            model = fit(kind, tr[cols], tr[target], va[cols], va[target])
            trees = model.best_iteration + 1
            preds_val[variant][kind] = pd.Series(predict(model, kind, val[cols]), index=val.index)
            preds_test[variant][kind] = pd.Series(predict(model, kind, test[cols]), index=test.index)
            out = pd.Series(predict(model, kind, oos[cols]), index=oos.index)

            # Refit on every completed game for upcoming games, with the tree count found above.
            final = fit(kind, done[done[target].notna()][cols], done[done[target].notna()][target], None, None, trees)
            if len(upcoming):
                out.loc[upcoming.index] = predict(final, kind, upcoming[cols])
            stored[variant][kind] = out
            joblib.dump({"model": final, "features": cols}, MODEL_DIR / f"{league}_{variant}_{kind}.joblib")

            if kind == "margin":
                importance = pd.Series(model.feature_importances_, index=cols).sort_values(ascending=False)
                top = ", ".join(f"{c} {v:.2f}" for c, v in importance.head(8).items())
                print(f"[{league}] {variant} margin model: {trees} trees; top features: {top}", flush=True)
    print(flush=True)

    report(league, f"validation {VALIDATION} (used for early stopping)", val, preds_val)
    report(league, f"test {VALIDATION + 1}-{int(df['season'].max())}", test, preds_test)
    write_predictions(conn, league, oos, stored)


def write_predictions(conn, league, oos, stored):
    rows = []
    for variant, preds in stored.items():
        for i, game_id in oos["game_id"].items():
            rows.append((league, game_id, variant, float(preds["win"][i]), float(preds["margin"][i]),
                         float(preds["total"][i]),
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
