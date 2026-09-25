"""College football EPA from cached ESPN play-by-play (espn_pbp.py), with an own EP model.

ESPN doesn't publish expected points for college games, so this fits one the usual way:
    1. Label every play with the next score in the same half, from the offense's side
       (+ if the offense scores it, - if the other team does, 0 if nobody scores).
    2. Fit EP = f(down, distance, yards to goal, seconds left in the half) on those labels
       (XGBoost), using EP_TRAIN_END and earlier seasons only.
    3. EPA per play = EP after - EP before (points scored count directly).

Per-team and per-QB aggregates go into team_game_stats / player_game_stats (source
espn_pbp) with the same stat names as the NFL, so features.py treats both leagues alike.
CFB has no player ids in the summaries, so QBs are keyed by team + passer name from the play text.

    python cfb_epa.py
"""

import argparse
import gzip
import json
import re
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from psycopg.types.json import Jsonb

from database import connect, init_db
from espn_pbp import CACHE_DIR

SOURCE = "espn_pbp"
EP_TRAIN_END = 2021
EP_FEATURES = ["down", "distance", "yards_to_goal", "half_seconds", "goal_to_go"]
MODEL_PATH = Path(__file__).parent / "models" / "cfb_ep.joblib"
# Garbage time (cfbfastR's convention): score margin beyond these by quarter.
GARBAGE_MARGIN = {1: 999, 2: 38, 3: 28, 4: 22}
PASSER = re.compile(r"^(.+?) (?:pass|sacked|scrambles|incomplete)", re.IGNORECASE)
# Scores are read from what the play says happened, not from the score fields: before ~2014
# ESPN attaches score changes a play late and types touchdowns as plain "Rush"/"Pass".
TOUCHDOWN = re.compile(r"\bTD\b|touchdown", re.IGNORECASE)
FIELD_GOAL = re.compile(r"field goal (?:is )?good|\bFG\b.*\bgood\b", re.IGNORECASE)
SAFETY = re.compile(r"\bsafety\b", re.IGNORECASE)
# A touchdown on these was scored by the team that didn't start the play with the ball.
DEFENSIVE_SCORE = re.compile(r"intercept|fumble|blocked|punt|kickoff|kick off|return", re.IGNORECASE)
POINTS = {"TD": 7.0, "FG": 3.0, "SF": 2.0}


def clock_seconds(value):
    try:
        minutes, seconds = str(value).split(":")
        return int(minutes) * 60 + int(float(seconds))
    except (ValueError, AttributeError):
        return np.nan


def play_kind(play_type):
    t = play_type.lower() if isinstance(play_type, str) else ""
    if "pass" in t or "sack" in t or "interception" in t:
        return "pass"
    if "rush" in t:
        return "rush"
    return None


def load_plays(league="cfb"):
    rows = []
    for path in sorted((CACHE_DIR / league).glob("*/*.json.gz")):
        season, game_id = int(path.parent.name), path.name.split(".")[0]
        with gzip.open(path, "rt") as f:
            game = json.load(f)
        home, away = game.get("home"), game.get("away")
        prev_home = prev_away = 0
        for i, p in enumerate(game["plays"]):
            # Scores never go down within a game; ESPN occasionally lists a play out of order.
            home_score = max(prev_home, p.get("homeScore") or 0)
            away_score = max(prev_away, p.get("awayScore") or 0)
            start = p.get("start") or {}
            rows.append((
                game_id, season, i, home, away, start.get("team"), p.get("period"), clock_seconds(p.get("clock")),
                start.get("down"), start.get("distance"), start.get("yardsToEndzone"),
                p.get("type"), bool(p.get("isPenalty")), (p.get("scoringType") or {}).get("abbreviation"),
                home_score - prev_home, away_score - prev_away, prev_home, prev_away, p.get("text"),
            ))
            prev_home, prev_away = home_score, away_score
    cols = ["game_id", "season", "seq", "home", "away", "offense", "period", "clock", "down", "distance",
            "yards_to_goal", "type", "penalty", "scoring_type", "home_pts", "away_pts", "home_before", "away_before", "text"]
    plays = pd.DataFrame(rows, columns=cols)
    # Older games list goal-to-go distance as 0; the real distance is the yards to the goal line.
    goal = plays["distance"].eq(0) & plays["down"].between(1, 4)
    plays.loc[goal, "distance"] = plays.loc[goal, "yards_to_goal"]
    plays["half"] = np.where(plays["period"] <= 2, 1, np.where(plays["period"] <= 4, 2, 3))  # 3 = overtime
    plays["half_seconds"] = plays["clock"] + np.where(plays["period"].isin([1, 3]), 900, 0)
    plays["goal_to_go"] = (plays["distance"] >= plays["yards_to_goal"]).astype(float)
    plays["kind"] = plays["type"].map(play_kind)
    plays["valid_state"] = (plays["down"].between(1, 4) & plays["yards_to_goal"].between(1, 99)
                            & plays["distance"].between(1, 99) & plays["offense"].notna() & (plays["half"] <= 2))
    return plays


def score_kind(play_type, text, scoring_type):
    """'TD', 'FG', 'SF' or None, from ESPN's scoring type, play type and play text."""
    play_type = play_type if isinstance(play_type, str) else ""
    main = (text if isinstance(text, str) else "").split("(")[0]  # "(X KICK)" etc. describe the PAT
    if scoring_type in ("TD", "FG", "SF"):
        return scoring_type
    if "extra point" in play_type.lower() or "two point" in play_type.lower() or "conversion" in play_type.lower():
        return None
    if "touchdown" in play_type.lower() or TOUCHDOWN.search(main):
        return "TD"
    if play_type == "Field Goal Good" or FIELD_GOAL.search(main):
        return "FG"
    if play_type == "Safety" or SAFETY.search(main):
        return "SF"
    return None


def score_events(plays):
    """Points each play put on the board, from the offense's side (+ offense scored, - defense did).

    offense = the team listed as starting the play (the kicking team on punts and kickoffs).
    """
    kinds = [score_kind(t, x, st) for t, x, st in zip(plays["type"], plays["text"], plays["scoring_type"])]
    kinds = pd.Series(kinds, index=plays.index)
    points = kinds.map(POINTS).fillna(0.0)
    text = plays["type"].fillna("") + " " + plays["text"].fillna("").str.split("(").str[0]
    by_defense = (kinds == "SF") | ((kinds == "TD") & text.str.contains(DEFENSIVE_SCORE))
    return points.where(~by_defense, -points), kinds.notna()


def next_score_labels(plays):
    """Next score in the same half, valued from each play's offense."""
    points, is_score = score_events(plays)
    offense_is_home = (plays["offense"] == plays["home"]).to_numpy()
    home_value = np.where(offense_is_home, points, -points)  # score event valued for the home team
    label = np.zeros(len(plays))
    pending = {}  # (game, half) -> home-team value of the next score seen so far (walking backwards)
    games, halves = plays["game_id"].to_numpy(), plays["half"].to_numpy()
    scores = is_score.to_numpy()
    for i in range(len(plays) - 1, -1, -1):
        key = (games[i], halves[i])
        if scores[i]:
            pending[key] = home_value[i]
        value = pending.get(key, 0.0)
        label[i] = value if offense_is_home[i] else -value
    return pd.Series(label, index=plays.index)


def fit_ep_model(plays):
    train = plays[plays["valid_state"] & (plays["season"] <= EP_TRAIN_END)]
    if train.empty:
        raise SystemExit(f"No cached plays through {EP_TRAIN_END}; run espn_pbp.py first.")
    model = xgb.XGBRegressor(n_estimators=400, max_depth=5, learning_rate=0.05, subsample=0.8, n_jobs=-1,
                             random_state=0)
    model.fit(train[EP_FEATURES], train["next_score"])
    MODEL_PATH.parent.mkdir(exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    print(f"[cfb_epa] EP model fit on {len(train)} plays through {EP_TRAIN_END}", flush=True)
    return model


def add_epa(plays, model):
    plays = plays.copy()
    plays["ep"] = np.where(plays["valid_state"], model.predict(plays[EP_FEATURES].astype(float)), np.nan)

    # EP after a play: the next valid state in the same game and half, flipped if possession changed.
    valid = plays[plays["valid_state"]]
    nxt = valid.groupby(["game_id", "half"])[["offense", "ep"]].shift(-1)
    plays["ep_after"] = np.nan
    same = nxt["offense"] == valid["offense"]
    plays.loc[valid.index, "ep_after"] = np.where(same, nxt["ep"], -nxt["ep"])
    plays.loc[valid.index[nxt["ep"].isna()], "ep_after"] = 0.0  # last play of the half

    points, is_score = score_events(plays)
    plays.loc[is_score, "ep_after"] = points[is_score]
    plays["epa"] = plays["ep_after"] - plays["ep"]
    plays["success"] = (plays["epa"] > 0).astype(float)
    return plays


def scrimmage(plays):
    """Runs and passes in competitive game states, excluding no-play penalties."""
    margin = (plays["home_before"] - plays["away_before"]).abs()
    competitive = margin <= plays["period"].map(GARBAGE_MARGIN).fillna(999)
    return plays[plays["valid_state"] & plays["kind"].notna() & ~plays["penalty"] & plays["epa"].notna() & competitive]


def team_stats(plays):
    plays = plays.assign(defense=np.where(plays["offense"] == plays["home"], plays["away"], plays["home"]),
                         is_pass=(plays["kind"] == "pass").astype(float), is_sack=plays["type"].eq("Sack").astype(float))
    out = []
    for side, prefix in (("offense", "off"), ("defense", "def")):
        g = plays.groupby(["game_id", side])
        passes, rushes = plays[plays["kind"] == "pass"], plays[plays["kind"] == "rush"]
        frame = pd.DataFrame({
            f"{prefix}_plays": g.size(),
            f"{prefix}_epa": g["epa"].mean(),
            f"{prefix}_success": g["success"].mean(),
            f"{prefix}_pass_epa": passes.groupby(["game_id", side])["epa"].mean(),
            f"{prefix}_rush_epa": rushes.groupby(["game_id", side])["epa"].mean(),
            f"{prefix}_pass_rate": g["is_pass"].mean(),
            f"{prefix}_sack_rate": passes.groupby(["game_id", side])["is_sack"].mean(),
            f"{prefix}_pass_success": passes.groupby(["game_id", side])["success"].mean(),
            f"{prefix}_rush_success": rushes.groupby(["game_id", side])["success"].mean(),
        }).rename_axis(["game_id", "team"])
        out.append(frame)
    return out[0].join(out[1], how="outer").reset_index()


def qb_stats(plays):
    passes = plays[plays["kind"] == "pass"].copy()
    passes["qb"] = passes["text"].fillna("").str.extract(PASSER, expand=False).str.strip()
    passes = passes[passes["qb"].notna() & (passes["qb"].str.len() < 40)]
    g = passes.groupby(["game_id", "offense", "qb"])
    return pd.DataFrame({"dropbacks": g.size(), "epa_sum": g["epa"].sum(), "success": g["success"].mean()}).reset_index()


def write(conn, team, qbs):
    team_rows = [
        ("cfb", r.pop("game_id"), r.pop("team"), SOURCE,
         Jsonb({k: (None if pd.isna(v) else round(float(v), 5)) for k, v in r.items()}))
        for r in team.to_dict("records")
    ]
    qb_rows = [
        ("cfb", r["game_id"], f"{r['offense']}:{r['qb']}", r["offense"], SOURCE, r["qb"],
         Jsonb({"dropbacks": float(r["dropbacks"]), "epa_sum": round(float(r["epa_sum"]), 5),
                "success": round(float(r["success"]), 5)}))
        for r in qbs.to_dict("records")
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM team_game_stats WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.execute("DELETE FROM player_game_stats WHERE league = 'cfb' AND source = %s", (SOURCE,))
        cur.executemany("INSERT INTO team_game_stats (league, game_id, team_id, source, stats) "
                        "VALUES (%s, %s, %s, %s, %s)", team_rows)
        cur.executemany("INSERT INTO player_game_stats (league, game_id, player_id, team_id, source, player_name, stats) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", qb_rows)
    print(f"[cfb_epa] {len(team_rows)} team-games, {len(qb_rows)} QB-games written", flush=True)


def main():
    argparse.ArgumentParser(description="Build CFB EPA from cached ESPN play-by-play.").parse_args()
    plays = load_plays()
    print(f"[cfb_epa] {len(plays)} plays from {plays['game_id'].nunique()} games", flush=True)
    plays["next_score"] = next_score_labels(plays)
    plays = add_epa(plays, fit_ep_model(plays))
    sc = scrimmage(plays)
    print(f"[cfb_epa] {len(sc)} scrimmage plays; mean EPA {sc['epa'].mean():+.3f}, "
          f"pass {sc.loc[sc['kind'] == 'pass', 'epa'].mean():+.3f}, rush {sc.loc[sc['kind'] == 'rush', 'epa'].mean():+.3f}",
          flush=True)
    with connect() as conn:
        init_db(conn)
        write(conn, team_stats(sc), qb_stats(sc))


if __name__ == "__main__":
    main()
