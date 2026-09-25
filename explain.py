"""What the models lean on, for the explainer page: share of the XGBoost margin model's total gain,
grouped into feature families. Runs after train.py (pipeline weekly) and writes model_explain.

    python explain.py
"""
import json
import re

import joblib

from database import connect, init_db

FAMILIES = [  # first match wins
    ("Form vs rating", r"vs_elo"),
    ("Elo rating", r"elo"),
    ("Recent scoring margin", r"ewm_margin|last3_margin|ewm_points"),
    ("Season scoring margin", r"season_margin"),
    ("Quarterback play", r"qb_"),
    ("Efficiency (EPA, success rate)", r"epa|success"),
    ("Recruiting and talent", r"recruit|talent"),
    ("Preseason outlook", r"preseason|prev_sp|returning"),
    ("Poll rank", r"_rank"),
    ("Injuries", r"starters_out|missing_"),
    ("Rest, travel, weather, schedule", r"rest|travel|dome|wind|temp|neutral|div_game|week|season_type"),
    ("Everything else", r""),
]


def family(feature):
    return next(name for name, pattern in FAMILIES if re.search(pattern, feature))


def explain(league):
    bundle = joblib.load(f"models/{league}_xgb_margin.joblib")
    gain = bundle["model"].get_booster().get_score(importance_type="total_gain")
    total = sum(gain.values())
    groups = {}
    for feature, g in gain.items():
        groups[family(feature)] = groups.get(family(feature), 0) + g / total
    return {
        "families": sorted(({"family": k, "share": v} for k, v in groups.items()), key=lambda r: -r["share"]),
        "features": [{"feature": k, "share": v / total} for k, v in sorted(gain.items(), key=lambda kv: -kv[1])[:15]],
        "n_features": len(bundle["features"]),
    }


def main():
    with connect() as conn:
        init_db(conn)
        for league in ("nfl", "cfb"):
            data = explain(league)
            conn.execute(
                "INSERT INTO model_explain (league, model, data) VALUES (%s, 'xgb_margin', %s) "
                "ON CONFLICT (league, model) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (league, json.dumps(data)))
            print(league, [(r["family"], round(r["share"], 3)) for r in data["families"]])


if __name__ == "__main__":
    main()
