"""Predict unplayed games with the saved models (no retraining).

train.py saves each variant's final model (refit on every completed game) to models/.
This reloads them and updates predictions for games that haven't been played yet, using
the latest features and lines, so frequent refreshes can keep upcoming predictions
current. Predictions for completed games are left as they were before kickoff.

    python predict.py
    python predict.py --league nfl
"""

import argparse

import joblib
import pandas as pd
from psycopg.types.json import Jsonb

from database import connect, init_db
from espn_client import LEAGUES
from train import MODEL_DIR, TARGETS, base_margin, load, predict, usable

VARIANTS = ["xgb", "xgb_market", "linear"]


def predict_league(conn, league):
    df, _ = load(conn, league)
    upcoming = df[~df["completed"] & (df["season"] == df["season"].max())]
    if upcoming.empty:
        print(f"[{league}] no unplayed games", flush=True)
        return

    rows = []
    for variant in VARIANTS:
        values = {kind: pd.Series(index=upcoming.index, dtype=float) for kind in TARGETS}
        for kind in TARGETS:
            path = MODEL_DIR / f"{league}_{variant}_{kind}.joblib"
            if not path.exists():
                continue
            saved = joblib.load(path)
            frame = usable(variant, kind, upcoming)
            if frame.empty:
                continue
            X = frame.reindex(columns=saved["features"])
            values[kind].loc[frame.index] = predict(saved["model"], kind, X, base_margin(variant, kind, frame))
        for i, game_id in upcoming["game_id"].items():
            triple = [None if pd.isna(values[k][i]) else float(values[k][i]) for k in ("win", "margin", "total")]
            if any(v is not None for v in triple):
                rows.append((league, game_id, variant, *triple, Jsonb({"out_of_sample": False, "source": "predict.py"})))

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO predictions (league, game_id, model, home_win_prob, predicted_margin, predicted_total, details) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (league, game_id, model) DO UPDATE SET home_win_prob = EXCLUDED.home_win_prob, "
            "predicted_margin = EXCLUDED.predicted_margin, predicted_total = EXCLUDED.predicted_total, "
            "details = EXCLUDED.details, created_at = now()",
            rows,
        )
    print(f"[{league}] {len(rows)} predictions for {len(upcoming)} unplayed games", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Predict unplayed games with the saved models.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        for league in args.league:
            predict_league(conn, league)


if __name__ == "__main__":
    main()
