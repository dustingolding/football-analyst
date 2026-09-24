"""Elo baseline: margin-of-victory Elo, evaluated against the closing line.

Every game gets a pre-game win probability and margin computed only from games that
finished before it, so the stored predictions are safe to use as model features.

Parameters are tuned on the train seasons, then the model is scored on the validation
and test seasons alongside the closing line (Vegas) on the same games.

    python elo.py                      # tune, evaluate, and write predictions for both leagues
    python elo.py --league nfl --no-tune
"""

import argparse
import itertools
import math
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from database import closing_lines, connect, init_db
from espn_client import LEAGUES

MEAN_RATING = 1500
FCS_RATING = 1200          # CFB teams outside FBS (only seen when they play an FBS team)
FBS_MIN_GAMES = 6          # scheduled games in our FBS-only scoreboard that mark a team as FBS that season

BURN_IN_END = 2007         # ratings need a few seasons to settle before they're scored
TRAIN_END = 2021
VALIDATION = 2022          # test = everything after

DEFAULT_PARAMS = {
    "nfl": {"k": 20, "hfa": 55, "regress": 0.33},
    "cfb": {"k": 30, "hfa": 70, "regress": 0.33},
}
GRID = {"k": [15, 20, 25, 30, 40, 50, 60], "hfa": [30, 45, 55, 70, 85], "regress": [0.2, 0.33, 0.5]}


def load_games(conn, league):
    rows = conn.execute(
        """
        SELECT game_id, season, start_time, neutral_site, home_team_id, away_team_id,
               home_score, away_score, completed
        FROM games
        WHERE league = %s AND start_time IS NOT NULL
        ORDER BY start_time, game_id
        """,
        (league,),
    ).fetchall()
    keys = ["game_id", "season", "start_time", "neutral", "home", "away", "home_score", "away_score", "completed"]
    return [dict(zip(keys, row)) for row in rows]


def load_lines(conn, league):
    """Closing line per game: home_spread and de-vigged home win probability (median across books)."""
    return {
        game_id: {"spread": line["spread"], "prob": line["home_prob"]}
        for game_id, line in closing_lines(conn, league).items()
    }


def fbs_by_season(games):
    counts = defaultdict(Counter)
    for g in games:
        counts[g["season"]][g["home"]] += 1
        counts[g["season"]][g["away"]] += 1
    return {season: {t for t, n in c.items() if n >= FBS_MIN_GAMES} for season, c in counts.items()}


def run_elo(league, games, params):
    """Walk games in kickoff order. Returns {game_id: pre-game prediction} and final ratings."""
    k, hfa, regress = params["k"], params["hfa"], params["regress"]
    fbs = fbs_by_season(games) if league == "cfb" else None
    ratings = {}
    season = None
    preds = {}

    def base(team, season):
        if fbs is None or team in fbs.get(season, ()):
            return MEAN_RATING
        return FCS_RATING

    for g in games:
        if g["season"] != season:
            season = g["season"]
            # Between seasons, pull each team part of the way back to its tier's mean.
            for team, rating in ratings.items():
                target = base(team, season)
                ratings[team] = rating + regress * (target - rating)

        home, away = g["home"], g["away"]
        rh = ratings.setdefault(home, base(home, season))
        ra = ratings.setdefault(away, base(away, season))
        diff = rh - ra + (0 if g["neutral"] else hfa)
        prob = 1 / (1 + 10 ** (-diff / 400))
        preds[g["game_id"]] = {"home_rating": rh, "away_rating": ra, "diff": diff, "prob": prob}

        if not g["completed"] or g["home_score"] is None or g["away_score"] is None:
            continue
        margin = g["home_score"] - g["away_score"]
        result = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        winner_diff = diff if margin >= 0 else -diff
        # 538-style multiplier: bigger wins move ratings more, but less so for heavy favorites.
        mult = math.log(abs(margin) + 1) * 2.2 / (winner_diff * 0.001 + 2.2)
        shift = k * mult * (result - prob)
        ratings[home] = rh + shift
        ratings[away] = ra - shift

    return preds, ratings


def scored(games, preds, seasons):
    for g in games:
        if g["season"] in seasons and g["completed"] and g["home_score"] is not None:
            yield g, preds[g["game_id"]]


def log_loss(pairs):
    eps = 1e-12
    return -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in pairs) / len(pairs)


def fit_margin_slope(games, preds, seasons):
    """Points of margin per Elo point, least squares through the origin."""
    xy = [(p["diff"], g["home_score"] - g["away_score"]) for g, p in scored(games, preds, seasons)]
    return sum(x * y for x, y in xy) / sum(x * x for x, _ in xy)


def evaluate(games, preds, lines, seasons, slope):
    """Metrics for Elo and the closing line on the same games (ties excluded from win metrics)."""
    rows = []
    for g, p in scored(games, preds, seasons):
        margin = g["home_score"] - g["away_score"]
        rows.append((g, p, margin, lines.get(g["game_id"])))
    decided = [r for r in rows if r[2] != 0]

    def win_metrics(items):
        pairs = [(p, 1.0 if m > 0 else 0.0) for p, m in items]
        if not pairs:
            return {}
        return {
            "log_loss": log_loss(pairs),
            "brier": sum((p - y) ** 2 for p, y in pairs) / len(pairs),
            "accuracy": sum((p > 0.5) == (y == 1) for p, y in pairs) / len(pairs),
        }

    out = {"games": len(rows)}
    out["elo"] = win_metrics([(p["prob"], m) for _, p, m, _ in decided])
    out["elo"]["margin_mae"] = sum(abs(m - slope * p["diff"]) for _, p, m, _ in rows) / len(rows)

    with_line = [r for r in rows if r[3]]
    with_prob = [r for r in decided if r[3] and r[3]["prob"] is not None]
    out["line_games"], out["prob_games"] = len(with_line), len(with_prob)
    if with_line:
        out["elo_on_line_games"] = win_metrics([(p["prob"], m) for _, p, m, _ in with_prob])
        out["elo_on_line_games"]["margin_mae"] = (
            sum(abs(m - slope * p["diff"]) for _, p, m, _ in with_line) / len(with_line))
        out["vegas"] = win_metrics([(line["prob"], m) for _, _, m, line in with_prob])
        out["vegas"]["margin_mae"] = sum(abs(m + line["spread"]) for _, _, m, line in with_line) / len(with_line)
        # Against the spread: when Elo's margin disagrees with the line, how often is Elo's side right?
        picks = [(slope * p["diff"] + line["spread"], m + line["spread"]) for _, p, m, line in with_line]
        picks = [(edge, cover) for edge, cover in picks if cover != 0]
        out["ats"] = sum((edge > 0) == (cover > 0) for edge, cover in picks) / len(picks) if picks else None
    return out


def tune(league, games):
    train = set(range(BURN_IN_END + 1, TRAIN_END + 1))
    best = None
    for k, hfa, regress in itertools.product(GRID["k"], GRID["hfa"], GRID["regress"]):
        params = {"k": k, "hfa": hfa, "regress": regress}
        preds, _ = run_elo(league, games, params)
        pairs = [(p["prob"], 1.0 if g["home_score"] > g["away_score"] else 0.0)
                 for g, p in scored(games, preds, train) if g["home_score"] != g["away_score"]]
        loss = log_loss(pairs)
        if best is None or loss < best[0]:
            best = (loss, params)
    print(f"[{league}] tuned on {BURN_IN_END + 1}-{TRAIN_END}: {best[1]} (train log loss {best[0]:.4f})", flush=True)
    return best[1]


def print_report(league, name, m):
    def fmt(metrics):
        return "  ".join(f"{k}={v:.4f}" if k != "margin_mae" else f"{k}={v:.2f}" for k, v in metrics.items())

    print(f"[{league}] {name}: {m['games']} games, {m['line_games']} with a closing line")
    print(f"    elo (all games)      {fmt(m['elo'])}")
    if m["line_games"]:
        print(f"    elo (line games)     {fmt(m['elo_on_line_games'])}")
        print(f"    vegas closing line   {fmt(m['vegas'])}")
        if m["ats"] is not None:
            print(f"    elo vs spread (ATS)  {m['ats']:.3f}  (52.4% needed to beat -110 juice)")
    print(flush=True)


def write_predictions(conn, league, games, preds, params, slope):
    rows = [
        (league, g["game_id"], "elo", p["prob"], slope * p["diff"],
         Jsonb({"home_rating": round(p["home_rating"], 1), "away_rating": round(p["away_rating"], 1),
                "params": params, "margin_per_elo": slope}))
        for g in games for p in [preds[g["game_id"]]]
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM predictions WHERE league = %s AND model = 'elo'", (league,))
        cur.executemany(
            "INSERT INTO predictions (league, game_id, model, home_win_prob, predicted_margin, details) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
    print(f"[{league}] wrote {len(rows)} elo predictions", flush=True)


def run_league(conn, league, do_tune):
    games = load_games(conn, league)
    lines = load_lines(conn, league)
    params = tune(league, games) if do_tune else DEFAULT_PARAMS[league]
    preds, ratings = run_elo(league, games, params)
    slope = fit_margin_slope(games, preds, set(range(BURN_IN_END + 1, TRAIN_END + 1)))

    last_season = max(g["season"] for g in games)
    print_report(league, f"validation {VALIDATION}", evaluate(games, preds, lines, {VALIDATION}, slope))
    print_report(league, f"test {VALIDATION + 1}-{last_season}",
                 evaluate(games, preds, lines, set(range(VALIDATION + 1, last_season + 1)), slope))

    write_predictions(conn, league, games, preds, params, slope)
    names = dict(conn.execute("SELECT team_id, display_name FROM teams WHERE league = %s", (league,)).fetchall())
    top = sorted(ratings.items(), key=lambda item: -item[1])[:10]
    print(f"[{league}] current top 10: " + ", ".join(f"{names.get(t, t)} {r:.0f}" for t, r in top), flush=True)
    print(flush=True)


def main():
    parser = argparse.ArgumentParser(description="Elo baseline, evaluated against the closing line.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    parser.add_argument("--no-tune", action="store_true", help="use DEFAULT_PARAMS instead of a grid search")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            run_league(conn, league, not args.no_tune)


if __name__ == "__main__":
    main()
