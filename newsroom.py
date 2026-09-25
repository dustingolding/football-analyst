"""Automated newsroom: game previews, recaps and weekly editorials written by a local LLM (Ollama) from fact
sheets built out of our own data and ESPN's game summary.

Every article is fact-checked against its fact sheet before anything is published:
  - every number in the headline, dek and body must appear in the fact sheet (or be a quarter/down 1-4),
  - every capitalized name must appear in the fact sheet (catches invented players, coaches, venues),
  - length and format limits, and no first-person or "as an AI" slips.
A draft that fails gets one rewrite with the problems listed; if it still fails it goes to the review queue
(/newsroom). With NEWSROOM_AUTOPUBLISH=1, previews and recaps that pass publish automatically;
editorials always wait for review.

    python newsroom.py                          # hourly: recaps, then previews, then (Tuesdays) editorials
    python newsroom.py --kind preview --league nfl --limit 2 --dry-run
    python newsroom.py --kind recap --game 401872948 --dry-run
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import live
from database import closing_lines, connect, init_db
from espn_client import EspnClient

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama.ai.svc.cluster.local:11434")
MODEL = os.getenv("NEWSROOM_MODEL", os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M"))
# Writer/verifier: OpenAI when OPENAI_KEY is set (NEWSROOM_PROVIDER=openai, the default then), else local Ollama.
# If OpenAI fails, the call falls back to Ollama.
OPENAI_KEY = os.getenv("OPENAI_KEY", "").strip().strip('"')
PROVIDER = os.getenv("NEWSROOM_PROVIDER", "openai" if OPENAI_KEY else "ollama")
OPENAI_MODELS = {"writer": os.getenv("NEWSROOM_OPENAI_MODEL", "gpt-5.5"),
                 "verify": os.getenv("NEWSROOM_OPENAI_VERIFY_MODEL", "gpt-5.4-mini")}
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "fallbacks": 0}
# 1: previews/recaps that pass every check publish on their own; 0: everything waits in /newsroom for review.
AUTOPUBLISH = os.getenv("NEWSROOM_AUTOPUBLISH", "0") == "1"
AUTOPUBLISH_RATINGS = AUTOPUBLISH and os.getenv("NEWSROOM_AUTOPUBLISH_RATINGS", "0") == "1"  # columns: review by default
LOCK_ID = 7352041  # separate from pipeline.py's lock: the newsroom can run alongside a refresh
EASTERN = ZoneInfo("America/New_York")
INDEPENDENT_MODEL = {"nfl": "linear", "cfb": "xgb"}
LEAGUE_NAMES = {"nfl": "NFL", "cfb": "college football"}
WORDS = {"preview": (180, 480), "recap": (180, 480), "editorial": (300, 850), "ratings": (350, 800)}

# ---------------------------------------------------------------------------------------------- facts


def rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def pct(p):
    return f"{round(p * 100)}%"


def spread_text(abbr_home, abbr_away, spread):
    """Home-perspective spread (-3.5 = home favored) as 'KC -3.5'."""
    if spread is None:
        return None
    if spread == 0:
        return "pick'em"
    fav = abbr_home if spread < 0 else abbr_away
    return f"{fav} -{abs(spread):g}"


def game_rows(conn, where, params):
    return rows(conn, f"""
        SELECT g.*, h.display_name AS home_name, h.short_name AS home_short, h.abbreviation AS home_abbr,
               a.display_name AS away_name, a.short_name AS away_short, a.abbreviation AS away_abbr
        FROM games g
        JOIN teams h ON h.league = g.league AND h.team_id = g.home_team_id
        JOIN teams a ON a.league = g.league AND a.team_id = g.away_team_id
        WHERE {where}""", params)


def team_ranks(conn, league, season):
    """Elo and opponent-adjusted offense/defense EPA ranks going into each team's next game."""
    feats = rows(conn, """
        WITH tg AS (
            SELECT gf.start_time, gf.completed, gf.features, g.home_team_id AS team, 'home' AS side
            FROM game_features gf JOIN games g USING (league, game_id) WHERE gf.league = %(l)s AND gf.season = %(s)s
            UNION ALL
            SELECT gf.start_time, gf.completed, gf.features, g.away_team_id, 'away'
            FROM game_features gf JOIN games g USING (league, game_id) WHERE gf.league = %(l)s AND gf.season = %(s)s)
        SELECT DISTINCT ON (team) team, side, features FROM tg
        ORDER BY team, completed, CASE WHEN completed THEN -extract(epoch FROM start_time)
                                       ELSE extract(epoch FROM start_time) END""", {"l": league, "s": season})
    if league == "cfb":  # rank among FBS teams (those with a preseason rating)
        fbs = {r["team_id"] for r in rows(conn, "SELECT team_id FROM team_preseason WHERE league = 'cfb' AND season = %s",
                                          (season,))}
        feats = [f for f in feats if f["team"] in fbs] if fbs else feats
    vals = {}
    for f in feats:
        x, side = f["features"], f["side"]
        vals[f["team"]] = {"elo": x.get(f"{side}_elo"), "off": x.get(f"{side}_ridge_off_epa"),
                           "def": x.get(f"{side}_ridge_def_epa")}
    out = {t: {} for t in vals}
    for key, reverse in (("elo", True), ("off", True), ("def", False)):  # defense: lower EPA allowed is better
        ranked = sorted((t for t in vals if vals[t][key] is not None), key=lambda t: vals[t][key], reverse=reverse)
        for i, t in enumerate(ranked, 1):
            out[t][key] = i
    return out, len(vals)


def recent_results(conn, league, team_id, season, before, n=2):
    games = game_rows(conn, "g.league = %s AND g.season = %s AND g.completed AND g.start_time < %s "
                            "AND %s IN (g.home_team_id, g.away_team_id) ORDER BY g.start_time DESC LIMIT %s",
                      (league, season, before, team_id, n))
    out = []
    for g in games:
        home = g["home_team_id"] == team_id
        us, them = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
        opp = g["away_short"] if home else g["home_short"]
        out.append(f"{'W' if us > them else 'L' if us < them else 'T'} {us}-{them} {'vs' if home else 'at'} {opp}")
    return out


def coach(conn, league, team_id, season):
    r = rows(conn, "SELECT coach FROM head_coaches WHERE league = %s AND team_id = %s AND season <= %s "
                   "ORDER BY season DESC LIMIT 1", (league, team_id, season))
    return r[0]["coach"] if r and league == "cfb" else None  # NFL coach data lags; leave coaches out rather than guess


def summary_team(summary, team_id):
    """Records, leaders, team stats and injuries for one side from ESPN's game summary."""
    out = {}
    for c in (summary.get("header") or {}).get("competitions", [{}])[0].get("competitors", []):
        if str(c.get("id") or (c.get("team") or {}).get("id")) == str(team_id):
            recs = {r.get("type"): r.get("summary") for r in c.get("record") or []}
            out["record"] = recs.get("total")
    for t in summary.get("leaders") or []:
        if str((t.get("team") or {}).get("id")) != str(team_id):
            continue
        leaders = {}
        for grp in t.get("leaders") or []:
            top = (grp.get("leaders") or [None])[0]
            if top and grp.get("name") in ("passingYards", "rushingYards", "receivingYards", "sacks"):
                leaders[grp["name"].replace("Yards", "")] = f"{top['athlete']['fullName']} ({top['displayValue']})"
        out["leaders"] = leaders
    for t in (summary.get("boxscore") or {}).get("teams") or []:
        if str((t.get("team") or {}).get("id")) == str(team_id):
            out["team_stats"] = {s["name"]: s["displayValue"] for s in t.get("statistics") or [] if s.get("name")}
    for t in summary.get("injuries") or []:
        if str((t.get("team") or {}).get("id")) != str(team_id):
            continue
        hurt = []
        for inj in t.get("injuries") or []:
            status = inj.get("status") or (inj.get("type") or {}).get("description")
            if status and status.lower() in ("out", "doubtful", "injured reserve"):
                a = inj.get("athlete") or {}
                pos = (a.get("position") or {}).get("abbreviation")
                hurt.append(f"{a.get('displayName')} ({pos}, {status})" if pos else f"{a.get('displayName')} ({status})")
        out["injuries"] = hurt[:5]
    return out


RECAP_STATS = {"totalYards": "total yards", "netPassingYards": "passing yards", "rushingYards": "rushing yards",
               "turnovers": "turnovers", "thirdDownEff": "third-down conversions", "possessionTime": "time of possession"}
PREVIEW_STATS = {"totalPointsPerGame": "points per game", "totalPointsPerGameAllowed": "points allowed per game",
                 "yardsPerGame": "yards per game", "yardsPerGameAllowed": "yards allowed per game"}
LEADER_LABELS = {"passing": "passing", "rushing": "rushing", "receiving": "receiving", "sacks": "sacks"}


def team_lines(conn, g, side, summary, ranks, n_teams, kind):
    """One statement per fact, each starting with the team's short name so numbers stay tied to their team."""
    tid, t = g[f"{side}_team_id"], g[f"{side}_short"]
    s = summary_team(summary, tid)
    r = ranks.get(tid, {})
    out = []
    if g[f"{side}_rank"]:
        out.append(f"{t} poll rank: No. {g[side + '_rank']}")
    if s.get("record"):
        out.append(f"{t} record{' after this game' if kind == 'recap' else ''}: {s['record']}")
    c = coach(conn, g["league"], tid, g["season"])
    if c:
        out.append(f"{t} head coach: {c}")
    if r.get("elo"):
        out.append(f"{t} SidelineWire power rating: No. {r['elo']} of {n_teams}")
    if r.get("off"):
        out.append(f"{t} offense efficiency: No. {r['off']} of {n_teams}")
    if r.get("def"):
        out.append(f"{t} defense efficiency: No. {r['def']} of {n_teams}")
    stats = s.get("team_stats") or {}
    if kind == "preview":
        results = recent_results(conn, g["league"], tid, g["season"], g["start_time"], n=6)
        if results:
            out.append(f"{t} last game: {results[0]}")
            if len(results) > 1:
                out.append(f"{t} game before that: {results[1]}")
            streak = 1
            while streak < len(results) and results[streak][0] == results[0][0]:
                streak += 1
            word = {"W": "won", "L": "lost", "T": "tied"}[results[0][0]]
            out.append(f"{t} current streak: {word} {streak} in a row" if streak > 1 else f"{t} current streak: {word} last game only")
        out += [f"{t} {label}: {stats[k]}" for k, label in PREVIEW_STATS.items() if k in stats]
        out += [f"{t} season {LEADER_LABELS[k]} leader (season totals, not per game): {v}"
                for k, v in (s.get("leaders") or {}).items()]
        if s.get("injuries"):
            out.append(f"{t} players out or doubtful: " + ", ".join(s["injuries"]))
    else:
        out += [f"{t} {label}: {stats[k]}" for k, label in RECAP_STATS.items() if k in stats]
        out += [f"{t} {LEADER_LABELS[k]} leader in this game: {v}" for k, v in (s.get("leaders") or {}).items()]
    return out


def prediction_lines(conn, g, recap=False):
    league, gid = g["league"], g["game_id"]
    line = closing_lines(conn, league, [gid]).get(gid) or {}
    preds = {r["model"]: r for r in rows(conn, "SELECT model, home_win_prob, predicted_margin, predicted_total "
                                               "FROM predictions WHERE league = %s AND game_id = %s", (league, gid))}
    ind = preds.get(INDEPENDENT_MODEL[league]) or {}
    h, a = g["home_short"], g["away_short"]
    out, info = [], {}
    if line.get("spread") is not None:
        sp = line["spread"]
        fav = h if sp < 0 else a
        info["vegas_fav"] = fav if sp else None
        out.append(f"Betting line{' before the game' if recap else ''}: {spread_text(g['home_abbr'], g['away_abbr'], sp)}"
                   + (f" ({fav} favored by {abs(sp):g})" if sp else ""))
    if line.get("total") is not None and not recap:
        out.append(f"Over/under: {line['total']:g}")
    margin, total = ind.get("predicted_margin"), ind.get("predicted_total")
    if ind.get("home_win_prob") is not None:
        p = ind["home_win_prob"]
        fav, prob = (h, p) if p >= 0.5 else (a, 1 - p)
        info["model_fav"], info["model_prob"] = fav, prob
        if margin is not None and (margin > 0) != (p >= 0.5):
            # the win and margin models disagree on the winner: say so rather than hand the writer a contradiction
            info["model_fav"] = None
            out.append(f"SidelineWire projection{' before the game' if recap else ''}: a toss-up (win probability gives "
                       f"{fav} {pct(prob)}, while its score projection slightly favors {h if margin > 0 else a})")
        else:
            out.append(f"SidelineWire projection{' before the game' if recap else ''}: {fav} {pct(prob)} to win")
    if margin is not None and total is not None:
        hs, as_ = round((total + margin) / 2), round((total - margin) / 2)
        out.append(f"Projected score{' before the game' if recap else ''}: {h} {hs}, {a} {as_}")
        if line.get("spread") is not None and not recap:
            edge = margin + line["spread"]
            if abs(edge) >= 1.5:
                out.append(f"Projection vs Vegas: SidelineWire likes {h if edge > 0 else a} more than Vegas does, by "
                           f"{abs(edge):.1f} points")
            else:
                out.append("Projection vs Vegas: SidelineWire's projection agrees closely with the betting line")
    return out, info


def matchup_line(g):
    kickoff = g["start_time"].astimezone(EASTERN)
    where = ", ".join(x for x in (g["venue_name"], g["venue_city"], g["venue_state"]) if x)
    rank = lambda side: f"No. {g[side + '_rank']} " if g[side + "_rank"] else ""  # noqa: E731
    site = " (neutral site)" if g["neutral_site"] else ""
    return (f"{rank('away')}{g['away_name']} {'vs.' if g['neutral_site'] else 'at'} {rank('home')}{g['home_name']}, "
            f"{kickoff.strftime('%A, %B %-d')}, {kickoff.strftime('%-I:%M %p ET')}" + (f", {where}{site}" if where else ""))


def week_label(g):
    return g["notes"] or ("Postseason" if g["season_type"] == 3 else f"Week {g['week']}")


def aliases(g):
    """Names the writer may use for each team, for the entity-anchored number check."""
    out = {}
    for side in ("home", "away"):
        names = {g[f"{side}_name"], g[f"{side}_short"], g[f"{side}_abbr"]}
        names |= {n.rsplit(" ", 1)[-1] for n in list(names) if n and " " in n}  # "Falcons" from "Atlanta Falcons"
        loc = g[f"{side}_name"].rsplit(" ", 1)[0] if g[f"{side}_name"] and " " in g[f"{side}_name"] else None
        if loc:
            names.add(loc)  # "Atlanta"
        out[g[f"{side}_short"]] = sorted(n for n in names if n)
    return out


def preview_facts(conn, g, summary):
    ranks, n = team_ranks(conn, g["league"], g["season"])
    pred, _ = prediction_lines(conn, g)
    return {
        "game": [f"{LEAGUE_NAMES[g['league']]} {g['season']}, {week_label(g)}", matchup_line(g)]
        + (["Conference game"] if g["league"] == "cfb" and g["conference_game"] else []),
        g["away_short"]: team_lines(conn, g, "away", summary, ranks, n, "preview"),
        g["home_short"]: team_lines(conn, g, "home", summary, ranks, n, "preview"),
        "prediction": pred,
        "_aliases": aliases(g),
    }


def scoring_runs(g, plays):
    """Longest unanswered scoring run: (team short name, points, from score, to score)."""
    best, cur = None, None
    prev = (0, 0)
    for p in plays:
        a, h = p.get("awayScore"), p.get("homeScore")
        if a is None or h is None:
            continue
        side = "home" if h > prev[1] else "away" if a > prev[0] else None
        if side is None:
            continue
        pts = (h - prev[1]) if side == "home" else (a - prev[0])
        if cur and cur[0] == side:
            cur = (side, cur[1] + pts, cur[2], (a, h))
        else:
            cur = (side, pts, prev, (a, h))
        if not best or cur[1] > best[1]:
            best = cur
        prev = (a, h)
    return best


def standing(away_pts, home_pts, h, a):
    """'Falcons lead 17-7' / 'tied 7-7' (leader's points first)."""
    if away_pts == home_pts:
        return f"tied {home_pts}-{away_pts}"
    lead, hi, lo = (h, home_pts, away_pts) if home_pts > away_pts else (a, away_pts, home_pts)
    return f"{lead} lead {hi}-{lo}"


def scoring_line(p, team, h, a):
    """ESPN scoring play -> 'Q2 0:59: Falcons touchdown, Austin Hooper 5-yard pass from Michael Penix Jr.; Falcons
    lead 17-7'."""
    q = (p.get("period") or {}).get("number") or 1
    qn = f"Q{q}" if q <= 4 else "OT"
    text = p.get("text") or ""
    extra = ""
    m = re.search(r"\s*\((.*)\)\s*$", text)
    if m:
        text = text[:m.start()]
        if "two-point" in m.group(1).lower():
            extra = " plus a two-point conversion" if "fail" not in m.group(1).lower() else " (two-point try failed)"
        elif "kick" in m.group(1).lower() and ("failed" in m.group(1).lower() or "blocked" in m.group(1).lower()):
            extra = " (extra point missed)"
    text = re.sub(r"(\d+) Yd", r"\1-yard", text).replace(" Rush", " run").replace(" Field Goal", " field goal")
    text = text.replace(" Interception Return", " interception return").replace(" Fumble Return", " fumble return")
    kind = ((p.get("scoringType") or {}).get("displayName") or ("Field Goal" if "field goal" in text else "Touchdown")).lower()
    return (f"{qn} {(p.get('clock') or {}).get('displayValue', '')}: {team} {kind}, {text.strip()}{extra}; "
            f"{standing(p.get('awayScore'), p.get('homeScore'), h, a)}")


def recap_facts(conn, g, summary, plays):
    ranks, n = team_ranks(conn, g["league"], g["season"])
    hs, as_ = g["home_score"], g["away_score"]
    h, a = g["home_short"], g["away_short"]
    win, lose, ws, ls = (h, a, hs, as_) if hs > as_ else (a, h, as_, hs)
    margin = ws - ls
    where = ", ".join(x for x in (g["venue_name"], g["venue_city"], g["venue_state"]) if x)
    ot = len(g["home_linescores"] or []) > 4
    played = g["start_time"].astimezone(EASTERN)
    game = [f"{LEAGUE_NAMES[g['league']]} {g['season']}, {week_label(g)}, played {played.strftime('%A, %B %-d')}",
            f"Final score: {win} {ws}, {lose} {ls}" + (" (overtime)" if ot else "") + (f", at {where}" if where else ""),
            f"Winning margin: {margin} points"]
    game.append("Type of game: " + ("one-score finish" if margin <= 8 else "comfortable win" if margin <= 16
                                    else "decisive win" if margin <= 27 else "blowout"))
    for side, name in (("away", a), ("home", h)):
        ls_ = g[f"{side}_linescores"] or []
        if ls_:
            out = ", ".join(f"{'Q' + str(i + 1) if i < 4 else 'OT'} {v}" for i, v in enumerate(ls_))
            game.append(f"{name} points by quarter: {out}")
    pred, info = prediction_lines(conn, g, recap=True)
    upset_by = [x for x in ("vegas_fav", "model_fav") if info.get(x) == lose]
    if upset_by:
        game.append(f"Upset: yes, {win} won as the underdog"
                    + (" (Vegas and SidelineWire's projection both favored " + lose + ")" if len(upset_by) == 2 else
                       " (Vegas favored " + lose + ")" if upset_by == ["vegas_fav"] else " (SidelineWire's projection favored " + lose + ")"))
    else:
        game.append(f"Upset: no, {win} was the favorite" if info.get("vegas_fav") == win else "Upset: no")
    sp = summary.get("scoringPlays") or []
    prev_score = (0, 0)
    run = scoring_runs(g, sp)
    if run and run[1] >= 14:
        team = h if run[0] == "home" else a
        game.append(f"Longest scoring run: {team} scored {run[1]} unanswered points, from "
                    f"{standing(run[2][0], run[2][1], h, a)} to {standing(run[3][0], run[3][1], h, a)}")
    scoring, half = [], None
    for p in sp:
        q = (p.get("period") or {}).get("number") or 1
        if q >= 3 and half is None:
            half = prev_score
        team = h if str((p.get("team") or {}).get("id")) == g["home_team_id"] else a
        scoring.append(scoring_line(p, team, h, a))
        prev_score = (p.get("awayScore"), p.get("homeScore"))
    if half is None and sp:  # no second-half scoring
        half = prev_score
    if half:
        game.append("Halftime score: " + standing(half[0], half[1], h, a))
    wp = [p for p in plays if p.get("home_win_prob") is not None]
    if wp:
        lowest = min((p["home_win_prob"] if hs > as_ else 1 - p["home_win_prob"]) for p in wp)
        game.append(f"{win} lowest win probability during the game: {pct(lowest)}")
    return {
        "game": game,
        "scoring plays, in order": scoring[:16],
        a: team_lines(conn, g, "away", summary, ranks, n, "recap"),
        h: team_lines(conn, g, "home", summary, ranks, n, "recap"),
        "before the game": pred,
        "_aliases": aliases(g),
    }


def editorial_facts(conn, league):
    """Weekly state of the league: our top 10 by Elo with records, movers, and teams beating projections."""
    season = rows(conn, "SELECT max(season) AS s FROM games WHERE league = %s AND completed", (league,))[0]["s"]
    ranks, n = team_ranks(conn, league, season)
    teams = {r["team_id"]: r for r in rows(conn, "SELECT team_id, display_name, short_name FROM teams "
                                                 "WHERE league = %s", (league,))}
    done = game_rows(conn, "g.league = %s AND g.season = %s AND g.completed AND g.season_type = 2 "
                           "ORDER BY g.start_time", (league, season))
    # the last fully finished week (ignore unfinished games older than 2 days: postponed/canceled)
    open_weeks = {r["week"] for r in rows(conn, "SELECT DISTINCT week FROM games WHERE league = %s AND season = %s "
                                                "AND season_type = 2 AND NOT completed AND start_time > now() - interval '2 days'",
                                          (league, season))}
    week = max((g["week"] for g in done if g["week"] not in open_weeks), default=None)
    done = [g for g in done if g["week"] <= (week or 0)]
    rec = {}
    for g in done:
        for side, other in (("home", "away"), ("away", "home")):
            w = rec.setdefault(g[f"{side}_team_id"], [0, 0])
            if g[f"{side}_score"] > g[f"{other}_score"]:
                w[0] += 1
            elif g[f"{side}_score"] < g[f"{other}_score"]:
                w[1] += 1
    # Elo a week ago: the pre-game rating for each team's most recent game
    last_elo = {}
    for r in rows(conn, """
            SELECT g.home_team_id, g.away_team_id, p.details FROM games g
            JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
            WHERE g.league = %s AND g.season = %s AND g.completed AND g.season_type = 2 AND g.week = %s""",
                  (league, season, week)):
        last_elo[r["home_team_id"]] = r["details"].get("home_rating")
        last_elo[r["away_team_id"]] = r["details"].get("away_rating")
    now_elo = {}
    for r in rows(conn, """
            SELECT DISTINCT ON (t) t, elo FROM (
                SELECT g.home_team_id AS t, (p.details->>'home_rating')::float AS elo, g.start_time FROM games g
                JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
                WHERE g.league = %(l)s AND g.season = %(s)s AND NOT g.completed
                UNION ALL
                SELECT g.away_team_id, (p.details->>'away_rating')::float, g.start_time FROM games g
                JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
                WHERE g.league = %(l)s AND g.season = %(s)s AND NOT g.completed) x
            ORDER BY t, start_time""", {"l": league, "s": season}):
        now_elo[r["t"]] = r["elo"]
    top = sorted((t for t in ranks if ranks[t].get("elo")), key=lambda t: ranks[t]["elo"])[:10]

    def line(t):
        name = teams[t]["display_name"]
        w, l_ = rec.get(t, [0, 0])
        return f"{ranks[t]['elo']}. {name} ({w}-{l_}), offense rank {ranks[t].get('off')}, defense rank {ranks[t].get('def')}"
    movers = sorted(((now_elo[t] - last_elo[t], t) for t in last_elo if t in now_elo and t in ranks), reverse=True)
    pre = {r["team_id"]: r for r in rows(conn, "SELECT team_id, rating, actual FROM team_preseason "
                                               "WHERE league = %s AND season = %s AND actual IS NOT NULL", (league, season))}
    surprise = sorted(pre.values(), key=lambda r: r["actual"] - r["rating"])
    fmt_pre = lambda r: (f"{teams[r['team_id']]['display_name']}: projected {r['rating']:+.1f} points vs average "  # noqa: E731
                         f"before the season, playing at {r['actual']:+.1f}")
    return {
        "league": LEAGUE_NAMES[league], "season": season, "after week": week, "teams ranked": n,
        "our top 10 by Elo rating": [line(t) for t in top],
        "biggest risers last week (Elo points gained)": [f"{teams[t]['display_name']} +{d:.0f}" for d, t in movers[:3] if d > 0],
        "biggest fallers last week (Elo points lost)": [f"{teams[t]['display_name']} {d:.0f}" for d, t in movers[::-1][:3] if d < 0],
        "most above preseason projection": [fmt_pre(r) for r in surprise[::-1][:3]] if surprise else [],
        "most below preseason projection": [fmt_pre(r) for r in surprise[:3]] if surprise else [],
    }


# ---------------------------------------------------------------------------------------------- power ratings


def last_finished_week(conn, league, season):
    """Latest regular-season week whose games are all final (unfinished games older than 2 days don't count)."""
    open_weeks = {r["week"] for r in rows(conn, "SELECT DISTINCT week FROM games WHERE league = %s AND season = %s "
                                                "AND season_type = 2 AND NOT completed AND start_time > now() - interval '2 days'",
                                          (league, season))}
    done = [r["week"] for r in rows(conn, "SELECT DISTINCT week FROM games WHERE league = %s AND season = %s "
                                          "AND season_type = 2 AND completed", (league, season))]
    return max((w for w in done if w not in open_weeks), default=None)


def ratings_table(conn, league):
    """Every team (NFL, or FBS) ranked by power rating, with the columns of the weekly Power Ratings article.
    Rating = points better or worse than an average team on a neutral field (Elo scaled by the Elo model's
    points-per-Elo). Rank change is against the previous week's article when there is one."""
    import web_data  # noqa: PLC0415 (only the ratings article needs the site's data helpers)
    season = rows(conn, "SELECT max(season) AS s FROM games WHERE league = %s AND completed", (league,))[0]["s"]
    week = last_finished_week(conn, league, season)
    power = {t: r for t, r in web_data.power_ratings(league, season).items() if web_data.is_major(league, t, season)}
    teams, aff, recs = web_data.teams(league), web_data.affiliations(league, season), web_data.records(league, season)
    per_elo = rows(conn, "SELECT (details->>'margin_per_elo')::float AS m FROM predictions WHERE league = %s AND model = 'elo' "
                         "ORDER BY created_at DESC LIMIT 1", (league,))[0]["m"]
    mean = sum(r["elo"] for r in power.values()) / len(power)

    def ranks(key, reverse=True):
        order = sorted((t for t in power if power[t].get(key) is not None), key=lambda t: power[t][key], reverse=reverse)
        return {t: i for i, t in enumerate(order, 1)}
    elo_rank, off_rank, def_rank = ranks("elo"), ranks("off"), ranks("def", reverse=False)

    games = rows(conn, """
        SELECT g.game_id, g.completed, g.season_type, g.week, g.home_team_id, g.away_team_id,
               (p.details->>'home_rating')::float AS hr, (p.details->>'away_rating')::float AS ar, p.home_win_prob
        FROM games g JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
        WHERE g.league = %s AND g.season = %s ORDER BY g.start_time""", (league, season))
    # strength of schedule so far: opponents' current ratings (non-major opponents at their pre-game rating)
    opp, exp_w, left, before_week = {}, {}, {}, {}
    for g in games:
        for side, other in (("home", "away"), ("away", "home")):
            t, o = g[f"{side}_team_id"], g[f"{other}_team_id"]
            if t not in power:
                continue
            if g["completed"]:
                o_elo = power[o]["elo"] if o in power else (g["ar"] if side == "home" else g["hr"])
                opp.setdefault(t, []).append(o_elo)
                if g["season_type"] == 2 and g["week"] == week:
                    before_week.setdefault(t, g["hr"] if side == "home" else g["ar"])
            elif g["season_type"] == 2 and g["home_win_prob"] is not None:
                p = g["home_win_prob"] if side == "home" else 1 - g["home_win_prob"]
                exp_w[t] = exp_w.get(t, 0) + p
                left[t] = left.get(t, 0) + 1
    sos = {t: sum(v) / len(v) for t, v in opp.items()}
    sos_rank = {t: i for i, t in enumerate(sorted(sos, key=lambda t: -sos[t]), 1)}

    prev = rows(conn, "SELECT facts->'_table' AS tbl FROM articles WHERE league = %s AND kind = 'ratings' AND season = %s "
                      "AND week < %s ORDER BY week DESC LIMIT 1", (league, season, week or 0))
    if prev and prev[0]["tbl"]:
        prev_rank = {r["team_id"]: r["rank"] for r in prev[0]["tbl"]}
    else:  # ratings going into last week's games (teams on a bye keep their current rating)
        last = {t: before_week.get(t, power[t]["elo"]) for t in power}
        prev_rank = {t: i for i, t in enumerate(sorted(last, key=lambda t: -last[t]), 1)}

    out = []
    for t in sorted(power, key=lambda t: elo_rank[t]):
        rec = recs.get(t) or {}
        w, l_ = rec.get("w", 0), rec.get("l", 0)
        a_ = aff.get(t) or {}
        group = (a_.get("division") or a_.get("conference")) if league == "nfl" else a_.get("conference")
        pw = w + exp_w.get(t, 0)
        pl = l_ + left.get(t, 0) - exp_w.get(t, 0)
        info = teams.get(t) or {}
        out.append({
            "rank": elo_rank[t], "prev_rank": prev_rank.get(t), "team_id": t, "name": info.get("display_name"),
            "short": info.get("short_name") or info.get("display_name"), "abbr": info.get("abbreviation"),
            "logo": info.get("logo"), "group": group, "record": rec.get("overall") or f"{w}-{l_}",
            "rating": round((power[t]["elo"] - mean) * per_elo, 1),
            "off_rank": off_rank.get(t), "def_rank": def_rank.get(t), "sos_rank": sos_rank.get(t),
            "proj": f"{round(pw)}-{round(pl)}" if left.get(t) else None,
        })
    return {"season": season, "week": week, "rows": out, "n": len(out)}


def result_phrase(conn, league, team_id, season):
    """The team's most recent result as prose: 'beat the Jaguars 20-13 at home', 'lost 41-34 at Mississippi State'."""
    g = game_rows(conn, "g.league = %s AND g.season = %s AND g.completed AND %s IN (g.home_team_id, g.away_team_id) "
                        "ORDER BY g.start_time DESC LIMIT 1", (league, season, team_id))
    if not g:
        return None
    g = g[0]
    home = g["home_team_id"] == team_id
    us, them = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
    opp = g["away_short"] if home else g["home_short"]
    where = "" if g["neutral_site"] else (" at home" if home else " on the road")
    if us > them:
        return f"beat {opp} {us}-{them}{where}"
    if us < them:
        return f"lost to {opp} {them}-{us}{where}"
    return f"tied {opp} {us}-{them}{where}"


def tier(rank, n):
    """Plain-English tier for an efficiency rank."""
    if rank is None:
        return ""
    if rank == 1:
        return "the best"
    for cut, word in ((5, "top five"), (10, "top 10"), (n // 4, "top quarter"), (n // 2, "top half"),
                      (n - n // 4, "bottom half"), (n - 5, "bottom quarter")):
        if rank <= cut:
            return word
    return "bottom five"


def ratings_facts(conn, league, table):
    """The column's fact sheet: the top of the table, movers with their last result, and what's next."""
    n, rows_ = table["n"], table["rows"]
    by_id = {r["team_id"]: r for r in rows_}
    season, week = table["season"], table["week"]
    top_n = 10 if league == "nfl" else 25

    def change(r):
        return (r["prev_rank"] - r["rank"]) if r["prev_rank"] else 0

    def line(r):
        mv = change(r)
        move = f"up {mv} from No. {r['prev_rank']}" if mv > 0 else f"down {-mv} from No. {r['prev_rank']}" if mv < 0 else "no change"
        return (f"No. {r['rank']} {r['name']} ({r['record']}): rating {r['rating']:+.1f} points vs an average team; "
                f"offense No. {r['off_rank']} ({tier(r['off_rank'], n)}), defense No. {r['def_rank']} "
                f"({tier(r['def_rank'], n)}); {move}")

    def last_result(t):
        return result_phrase(conn, league, t, season)

    movers = sorted((r for r in rows_ if r["prev_rank"]), key=change)
    risers = [r for r in movers[::-1] if change(r) > 0][:5]
    fallers = [r for r in movers if change(r) < 0][:5]
    mover_line = lambda r: f"{r['name']}: No. {r['prev_rank']} to No. {r['rank']}" + (  # noqa: E731
        f"; last game: {last_result(r['team_id'])}" if last_result(r["team_id"]) else "")
    facts = {
        "report": [f"SidelineWire power ratings, {LEAGUE_NAMES[league]} {season}, after Week {week}",
                   f"{n} teams rated; rating = points better or worse than an average team on a neutral field"],
        f"top {top_n}": [line(r) for r in rows_[:top_n]],
        "biggest risers this week": [mover_line(r) for r in risers],
        "biggest fallers this week": [mover_line(r) for r in fallers],
    }
    if league == "cfb":
        prev_top = {r["team_id"] for r in rows_ if r["prev_rank"] and r["prev_rank"] <= 25}
        now_top = {r["team_id"] for r in rows_[:25]}
        facts["new to the top 25"] = [f"{by_id[t]['name']} (No. {by_id[t]['rank']})" for t in now_top - prev_top]
        facts["fell out of the top 25"] = [f"{by_id[t]['name']} (now No. {by_id[t]['rank']})" for t in prev_top - now_top]
    unbeaten_low = [r for r in rows_ if r["record"].endswith("-0") and r["rank"] > top_n][:4]
    if unbeaten_low:
        facts["unbeaten but outside the top " + str(top_n)] = [f"{r['name']} ({r['record']}), No. {r['rank']}" for r in unbeaten_low]
    losing_high = [r for r in rows_[:top_n] if int(r["record"].split("-")[1]) > int(r["record"].split("-")[0])]
    if losing_high:
        facts[f"losing record but inside the top {top_n}"] = [f"{r['name']} ({r['record']}), No. {r['rank']}" for r in losing_high]
    tough = sorted((r for r in rows_ if r["sos_rank"]), key=lambda r: r["sos_rank"])[:3]
    facts["toughest schedules so far"] = [f"{r['name']}: schedule strength No. {r['sos_rank']}, record {r['record']}" for r in tough]
    nxt = rows(conn, """
        SELECT g.home_team_id, g.away_team_id, p.home_win_prob FROM games g
        JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = 'elo'
        WHERE g.league = %s AND g.season = %s AND NOT g.completed AND g.season_type = 2 AND g.week = %s""",
                (league, season, (week or 0) + 1))
    big = sorted((g for g in nxt if g["home_team_id"] in by_id and g["away_team_id"] in by_id),
                 key=lambda g: by_id[g["home_team_id"]]["rank"] + by_id[g["away_team_id"]]["rank"])[:3]
    facts["biggest games next week"] = [
        f"No. {by_id[g['away_team_id']]['rank']} {by_id[g['away_team_id']]['name']} at No. {by_id[g['home_team_id']]['rank']} "
        f"{by_id[g['home_team_id']]['name']}: SidelineWire projection "
        + (f"{by_id[g['home_team_id']]['short']} {pct(g['home_win_prob'])}" if g["home_win_prob"] >= 0.5
           else f"{by_id[g['away_team_id']]['short']} {pct(1 - g['home_win_prob'])}") for g in big]
    facts = {k: v for k, v in facts.items() if v}
    mentioned = {r["team_id"] for r in rows_[:top_n]} | {r["team_id"] for r in risers + fallers + tough + unbeaten_low}
    names = {}
    for t in mentioned:
        r = by_id[t]
        names[r["short"]] = [x for x in {r["name"], r["short"], r["abbr"], r["name"].rsplit(" ", 1)[-1]} if x]
    # drop nicknames shared by two mentioned teams (Bulldogs, Tigers...), so a number can't be pinned on the wrong one
    counts = {}
    for al in names.values():
        for x in al:
            counts[x] = counts.get(x, 0) + 1
    facts["_aliases"] = {k: [x for x in v if counts[x] == 1] for k, v in names.items()}
    facts["_table"] = rows_
    return facts


def run_ratings(conn, league, dry_run, force=False):
    table = ratings_table(conn, league)
    key = f"ratings-{table['season']}-w{table['week']}"
    if not force and rows(conn, "SELECT 1 FROM articles WHERE league = %s AND kind = 'ratings' AND topic_key = %s",
                          (league, key)):
        return
    facts = ratings_facts(conn, league, table)
    article, problems, attempts = write("ratings", facts)
    if dry_run:
        print(facts_text(facts))
        print(f"== ratings {league} {key} problems={problems}")
        print(json.dumps(article, indent=1, ensure_ascii=False) if isinstance(article, dict) else article)
        return
    if force:
        conn.execute("DELETE FROM articles WHERE league = %s AND kind = 'ratings' AND topic_key = %s", (league, key))
    status, slug = save(conn, league, "ratings", None, article, facts, problems, attempts, AUTOPUBLISH_RATINGS,
                        table["season"], table["week"], key)
    print(f"[newsroom] ratings {league} {key}: {status} {slug} {problems or ''}", flush=True)


# ---------------------------------------------------------------------------------------------- writing

STYLE = """You are a sports writer for SidelineWire, a football analytics site. Write in clear, lively AP style.
Rules you must follow:
- Use ONLY the facts provided. Do not add players, coaches, stats, injuries, history, quotes or storylines that are not in the facts.
- Every number you write must appear in the facts exactly as given. Do not calculate new numbers (no differences, sums or averages). Write numbers as digits.
- No quotes from anyone. No first person. No headings, bold text or lists.
- Each fact line names the team or player it belongs to. Never attribute a number to a different team or player.
- Set the tone from the "Type of game" and "Upset" lines: call a game close only if it was a one-score finish.
- Refer to teams by the names given (nicknames and school names both work).
- Voice: a beat writer for a major sports site. Use natural football vernacular (ground game, pass rush,
  signal-caller, red zone, took care of business, statement win, trap game) where it fits the facts.
- SidelineWire's ratings and projections are context, not the subject. Mention them at most twice in a game
  story, and vary how: "the SidelineWire projection", "the power ratings", "the numbers", "the efficiency
  numbers". Never write "our model". Don't explain methodology or repeat terms like "EPA per play".
- Write like a newspaper sportswriter, not a stat sheet: tell a story with a clear angle, vary sentence length,
  and choose the few details that matter. Don't walk through every scoring play or list every stat.
- Put stats into prose: "194 yards on 29 carries", "18 of 25 for 256 yards", never "29 CAR" or "18/25, 256 YDS".
  Mention game-clock times only when the timing is the story.
Return JSON: {"headline": "...", "dek": "one-sentence summary", "body": "the article as plain paragraphs separated by blank lines"}"""

TASKS = {
    "preview": "Write a game preview of {lo}-{hi} words: the matchup, what each team does well, key players, "
               "injuries if listed, the betting line and where the SidelineWire projection lands.",
    "recap": "Write a game recap of {lo}-{hi} words. Lead with the result and the story of the game, cover the "
             "turning points in order (any score you give must match the scoring plays), the key performers and the "
             "stats that decided it, and how it compared with the pre-game expectations.",
    "ratings": "Write the column that introduces this week's power ratings, {lo}-{hi} words, like the lead-in to "
               "a major site's weekly rankings: a headline with a clear angle, then who's on top and why, the biggest "
               "risers and fallers and what they did, anything surprising (unbeaten teams ranked low, teams ranked "
               "high despite their record), and what to watch next week. The full table runs below the column, so "
               "don't walk through it team by team. Use a number only when it makes a point (usually one per team, "
               "never the full rating/offense/defense line); describe units in words like 'a top-five defense' or "
               "'an offense that ranks near the bottom'. Vary sentence openings.",
    "editorial": "Write a weekly column of {lo}-{hi} words on the state of the league according to our ratings: "
                 "who is on top and why, who moved, and which teams are beating or missing their preseason "
                 "projections. Give it a point of view, but stay within the facts.",
}


def ask(messages, temperature=0.3, role="writer"):
    """Chat in JSON mode with the configured provider; OpenAI failures fall back to the local model."""
    if PROVIDER == "openai" and OPENAI_KEY:
        try:
            return ask_openai(messages, role)
        except (requests.RequestException, KeyError, ValueError) as e:
            USAGE["fallbacks"] += 1
            print(f"[newsroom] OpenAI failed ({e!r:.200}); using local model", flush=True)
    return ask_ollama(messages, temperature)


def ask_openai(messages, role):
    for attempt in range(3):
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=(5, 300),
                          headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                          json={"model": OPENAI_MODELS[role], "messages": messages, "max_completion_tokens": 6000,
                                "response_format": {"type": "json_object"}})
        if r.status_code in (429, 500, 502, 503) and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    j = r.json()
    USAGE["calls"] += 1
    USAGE["input_tokens"] += j.get("usage", {}).get("prompt_tokens", 0)
    USAGE["output_tokens"] += j.get("usage", {}).get("completion_tokens", 0)
    content = j["choices"][0]["message"]["content"] or ""
    try:
        return json.loads(content), content
    except json.JSONDecodeError:
        return None, content


def ask_ollama(messages, temperature=0.3):
    """Chat in JSON mode. A 500 "token repeat limit reached" means the model looped; retry warmer."""
    for temp in (temperature, temperature + 0.3):
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=(5, 600), json={
            "model": MODEL, "stream": False, "format": "json", "messages": messages,
            "options": {"temperature": temp, "repeat_penalty": 1.08, "num_ctx": 8192, "num_predict": 1400}})
        if r.status_code == 500 and "repeat" in r.text:
            continue
        r.raise_for_status()
        break
    else:
        return None, ""
    content = r.json()["message"]["content"]
    try:
        out = json.loads(content)
    except json.JSONDecodeError:
        return None, content
    return out, content


def facts_text(facts):
    """The fact sheet as the writer sees it: sections of one-line statements (keys starting with _ are internal)."""
    out = []
    for key, value in facts.items():
        if key.startswith("_"):
            continue
        out.append(f"{key.upper()}:")
        if isinstance(value, list):
            out += [f"- {v}" for v in value]
        else:
            out.append(f"- {value}")
    return "\n".join(out)


def leaves(facts):
    return [ln[2:] for ln in facts_text(facts).splitlines() if ln.startswith("- ")]

# ---------------------------------------------------------------------------------------------- checking

NUMBER = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?")
SCORE = re.compile(r"\b(\d+)\s*[-–]\s*(\d+)\b")
NAME_WORD = re.compile(r"\b[A-Z][\w'’.\-]*[A-Za-z]|\b[A-Z]\.[A-Z]\.")
ALLOWED_WORDS = {
    "NFL", "AFC", "NFC", "FBS", "FCS", "SEC", "ACC", "Big", "Ten", "Twelve", "Pac-12", "Mountain", "West", "Sun", "Belt",
    "American", "Conference", "USA", "Mid-American", "Independents", "AP", "Top", "Vegas", "Elo", "EPA", "SidelineWire",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November", "December", "Week", "ET",
    "Q1", "Q2", "Q3", "Q4", "OT", "TD", "TDs", "QB", "QBs", "RB", "WR", "TE", "Over", "Under", "Super", "Bowl",
    "College", "Football", "Playoff", "National", "Championship", "Heisman", "Game", "Day",
}
SENTENCE_STARTERS = {
    "The", "A", "An", "In", "On", "At", "After", "Before", "With", "Without", "But", "And", "Yet", "So", "When", "While",
    "Although", "Though", "Despite", "Meanwhile", "However", "Still", "Then", "This", "That", "These", "Those", "It",
    "Its", "Their", "They", "His", "He", "For", "If", "As", "By", "From", "Both", "Neither", "Either", "Our", "Expect",
    "Look", "Watch", "Keep", "Behind", "Led", "Leading", "Heading", "Coming", "Entering", "Through", "Over", "Under",
    "Against", "Early", "Late", "Down", "Up", "Trailing", "Facing", "Only", "Even", "Not", "No", "All", "Each", "Every",
    "One", "Two", "Three", "Four", "Five", "What", "Who", "Why", "How", "Where", "Can", "Will", "Could", "Should",
    "Here", "There", "Now", "Next", "Last", "First", "Second", "Third", "Fourth", "Final", "Overall", "Offensively",
    "Defensively", "Ultimately", "Instead", "Also", "Plus", "Such", "Much", "More", "Most", "Few", "Many", "Several",
    "Key", "Kickoff", "Sunday's", "Saturday's", "Thursday's", "Monday's", "Friday's", "Tonight's", "Game", "Fans",
    "Look", "Beyond", "Along", "Amid", "Given", "Until", "Since", "Because", "Winning", "Losing", "Turnovers",
    "Injuries", "Injury", "Defense", "Offense", "Special", "Quarterback", "Running", "Receiver", "Coach", "Head",
    "Bettors", "Oddsmakers", "Sportsbooks", "Projected", "Predicted", "According", "Notably", "Additionally",
    "Furthermore", "Moreover", "Similarly", "Conversely", "Nevertheless", "Nonetheless", "Regardless", "Otherwise",
    "Currently", "Recently", "Historically", "Statistically", "Around", "Across", "Among", "Between", "During",
    "Within", "Toward", "Towards", "Unlike", "Like", "Following", "Including", "Rounding", "Closing", "Opening",
}
def _common_words():
    """Lowercase English words (Debian wamerican), so ordinary capitalized words aren't mistaken for names."""
    try:
        with gzip.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "newsroom_words.txt.gz"), "rt") as f:
            return {w.strip() for w in f}
    except OSError:
        return set()


COMMON_WORDS = _common_words()
BRAND_CAP = {"preview": 2, "recap": 2, "editorial": 4, "ratings": 4}
# Stat words after a number -> what the supporting fact line must contain (ESPN abbreviates carries/catches).
STAT_STEMS = {"rush": ("rush", "car"), "pass": ("pass",), "receiv": ("receiv", "rec"), "total": ("total",),
              "turnover": ("turnover",), "sack": ("sack",), "third": ("third",), "interception": ("int",),
              "tackle": ("tackle",), "carr": ("car",), "catch": ("rec", "catch"), "reception": ("rec",)}

BANNED = re.compile(r"\bas an ai\b|\blanguage model\b|\bI (?:think|believe|cannot)\b|\bin conclusion\b|\[.*?\]|"
                    r"\{|\}|https?://", re.I)


def norm_num(tok):
    t = tok.replace(",", "").replace("−", "-").lstrip("+")
    try:
        v = float(t)
    except ValueError:
        return tok
    return f"{abs(v):g}"


def allowed_numbers(facts):
    nums = {norm_num(m) for m in NUMBER.findall(facts_text(facts))}
    return nums | {"1", "2", "3", "4"}  # quarters, downs


SUFFIXES = {"Jr.", "Sr.", "II", "III", "IV", "V"}


def entities(facts):
    """alias (lowercase) -> (entity, phrases that tie a fact line to it). Teams from _aliases, players from
    'leader' lines."""
    out = {}
    for team, names in (facts.get("_aliases") or {}).items():
        for n in names:
            out[n.lower()] = (team, [x.lower() for x in names])
    for leaf in leaves(facts):
        for m in re.finditer(r"leader(?: in this game)?: ([^()]+?) \(", leaf):
            full = m.group(1).strip()
            parts = [w for w in full.split() if w not in SUFFIXES]
            for alias in {full, parts[-1]} if parts else {full}:
                out.setdefault(alias.lower(), ("player:" + full, [full.lower()]))
    return out


def anchored_number_problems(text, facts):
    """Each number must appear in a fact line that also names the team or player mentioned closest before it in
    the same sentence (so '328 yards' can't be pinned on the wrong team)."""
    ents = entities(facts)
    if not ents:
        return []
    lines = [(ln.lower(), {norm_num(x) for x in NUMBER.findall(ln)}) for ln in leaves(facts)]
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(a) for a in sorted(ents, key=len, reverse=True)) + r")(?!\w)", re.I)
    problems = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        marks = sorted([(m.start(), "ent", m.group(1).lower()) for m in pattern.finditer(sentence)]
                       + [(m.start(), "num", m.group(0)) for m in NUMBER.finditer(sentence)])

        def supported(n, entity):
            _, phrases = entity
            return any(n in nums and any(re.search(r"(?<!\w)" + re.escape(ph) + r"(?!\w)", ln) for ph in phrases)
                       for ln, nums in lines)
        for i, (_, kind, val) in enumerate(marks):
            if kind == "ent":
                continue
            n = norm_num(val)
            clock = re.match(r"\d{1,2}:\d\d", sentence[marks[i][0]:]) or (
                marks[i][0] > 0 and sentence[marks[i][0] - 1] == ":")
            if clock:  # times like 37:05 (possession, game clock) are checked whole, below
                continue
            before = next((ents[v] for _, k, v in reversed(marks[:i]) if k == "ent"), None)
            after = next((ents[v] for _, k, v in marks[i + 1:] if k == "ent"), None)
            if n in {"1", "2", "3", "4"} or re.fullmatch(r"20\d\d", val) or before is None:
                continue
            name = before[0]
            pos = marks[i][0]
            following = " ".join(sentence[pos + len(val):].split()[:3]).lower()
            if re.match(r"(?:\w+\s+){0,2}(?:per game|a game|per contest|a contest|per outing)", following) and not any(
                    n in nums and "per game" in ln for ln, nums in lines):
                problems.append(f"{val} is not a per-game figure in the facts; season totals are not averages "
                                f"(in: \"{sentence.strip()[:90]}\")")
                continue
            play_distance = re.match(r"\s?-?\s?yard\b", sentence[pos + len(val):])  # "15-yard pass": a play, not a stat
            stem = None if play_distance else next((st for st in STAT_STEMS if st in following), None)
            if stem and not any(n in nums and any(k in ln for k in STAT_STEMS[stem]) and any(re.search(r"(?<!\w)" + re.escape(ph) + r"(?!\w)", ln)
                                                                 for ph in before[1] + (after[1] if after else []))
                                for ln, nums in lines):
                problems.append(f"{val} {following.split()[0] if following else ''} is not a {name.removeprefix('player:')} "
                                f"'{stem}' figure in the facts (in: \"{sentence.strip()[:90]}\")")
                continue
            if not supported(n, before) and not (after and supported(n, after)):
                problems.append(f"{val} is not a {name.removeprefix('player:')} figure in the facts "
                                f"(in: \"{sentence.strip()[:90]}\")")
    return problems[:8]


def allowed_pairs(facts):
    """Every 'X-Y' the facts support: score states ('Falcons 7, Packers 0'), records, stat pairs ('6-10', '18/25')."""
    text = facts_text(facts)
    pairs = set()
    for m in re.finditer(r"(\d+)\s*[-/]\s*(\d+)", text):
        pairs.add(frozenset((m.group(1), m.group(2))) if m.group(1) != m.group(2) else frozenset((m.group(1),)))
    for m in re.finditer(r"[A-Z][\w.'’ ]*? (\d+), [A-Z][\w.'’ ]*? (\d+)", text):
        pairs.add(frozenset((m.group(1), m.group(2))) if m.group(1) != m.group(2) else frozenset((m.group(1),)))
    return pairs


def pair_problems(text, facts):
    allowed = allowed_pairs(facts)
    bad = []
    for m in re.finditer(r"(?<![\d.])(\d+)\s*[-–]\s*(\d+)(?![\d.])", text):
        x, y = m.group(1), m.group(2)
        key = frozenset((x, y)) if x != y else frozenset((x,))
        if key not in allowed:
            bad.append(f"{x}-{y}")
    return [f"these scores or records never appear in the facts: {', '.join(sorted(set(bad)))}"] if bad else []


VERIFY = """You are a strict fact-checker for a sports site. Compare the ARTICLE with the FACTS.
Find statements that are FALSE or NOT IN THE FACTS: wrong numbers, numbers credited to the wrong team or player or
the wrong stat (total yards called rushing yards), wrong quarter or order of events, the wrong kind of score, and claims
the facts don't contain (streaks, history, first win, injuries, quotes). Do not list statements that are correct.
Ignore opinions, adjectives and style.
Return JSON: {"errors": [{"quote": "the exact sentence from the article", "problem": "what is wrong"}]}
Use an empty list if everything is supported."""

CONFIRM = """You check one claim from a sports article against the FACTS. Decide whether every factual detail in
the CLAIM (numbers, teams, players, which stat, order of events) is supported by the FACTS. Opinions and adjectives
don't matter. Return JSON: {"supported": true or false, "detail": "short reason"}"""


def verify(article, facts):
    """Two-stage LLM fact check: list suspect sentences, then confirm each one separately (a 7B model's first
    pass flags plenty of correct sentences). Returns the confirmed problems."""
    text = f"HEADLINE: {article['headline']}\nDEK: {article['dek']}\n\n{article['body']}"
    sheet = facts_text(facts)
    out, _ = ask([{"role": "system", "content": VERIFY},
                  {"role": "user", "content": sheet + "\n\nARTICLE:\n" + text}], temperature=0, role="verify")
    errors = (out or {}).get("errors") if isinstance(out, dict) else None
    if errors is None:
        return ["fact-check pass did not return a result"]
    confirmed = []
    for e in errors[:8]:
        quote = (e.get("quote") if isinstance(e, dict) else str(e)) or ""
        problem = (e.get("problem") if isinstance(e, dict) else "") or ""
        if not quote.strip() or re.search(r"\bcorrect\b", problem, re.I) and not re.search(r"incorrect|not correct", problem, re.I):
            continue
        res, _ = ask([{"role": "system", "content": CONFIRM},
                      {"role": "user", "content": f"{sheet}\n\nCLAIM: {quote}"}], temperature=0, role="verify")
        if isinstance(res, dict) and res.get("supported") is False:
            confirmed.append(f"fact-check: \"{quote[:140]}\" - {res.get('detail') or problem}")
    return confirmed[:6]


ORDINALS = {w: str(i) for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split(), start=1)}


def ordinals_to_digits(text):
    """'ranks seventh' -> 'ranks 7', so spelled-out ranks are checked like digits."""
    return re.sub(r"\b(" + "|".join(ORDINALS) + r")\b", lambda m: ORDINALS[m.group(1).lower()], text, flags=re.I)


def normalize_body(body):
    """Paragraphs separated by blank lines; drop markdown headings and bold-only header lines."""
    lines = [ln.strip() for ln in body.strip().splitlines()]
    lines = [ln for ln in lines if not re.fullmatch(r"#+ .*|\*\*[^*]+\*\*:?", ln)]
    return "\n\n".join(ln for ln in lines if ln)


def check(kind, article, facts):
    """Return a list of problems (empty = passed)."""
    problems = []
    if not isinstance(article, dict) or not all(isinstance(article.get(k), str) and article[k].strip()
                                                for k in ("headline", "dek", "body")):
        return ["response was not JSON with headline, dek and body"]
    article["body"] = normalize_body(article["body"])
    headline, dek, body = article["headline"].strip(), article["dek"].strip(), article["body"]
    text = ordinals_to_digits(f"{headline}\n{dek}\n{body}")
    words = len(body.split())
    lo, hi = WORDS[kind]
    if not lo <= words <= hi:
        problems.append(f"body is {words} words; it must be {lo}-{hi}")
    if len(headline) > 110:
        problems.append("headline is too long (max 110 characters)")
    if body.count("\n\n") < 2:
        problems.append("body needs at least 3 paragraphs separated by blank lines")
    if BANNED.search(text):
        problems.append(f"remove this phrase or markup: {BANNED.search(text).group(0)!r}")
    brand = len(re.findall(r"\bour (?:model|ratings?|numbers|projections?|metrics)\b|\bSidelineWire\b|\bthe model\b", text, re.I))
    cap = BRAND_CAP.get(kind, 2)
    if brand > cap:
        problems.append(f"mentions SidelineWire/the model/our ratings {brand} times; use at most {cap} and let the "
                        "football carry the story")
    if re.search(r"\bour model\b", text, re.I):
        problems.append('don\'t write "our model"; say "the SidelineWire projection" or "the power ratings" (sparingly)')
    if '"' in body or "“" in body:
        problems.append("no quotations: the facts contain no quotes")

    allowed = allowed_numbers(facts)
    bad = sorted({m for m in NUMBER.findall(text) if norm_num(m) not in allowed and not re.fullmatch(r"20\d\d", m)})
    if bad:
        problems.append("numbers not in the facts: " + ", ".join(bad[:12]))
    else:
        problems += anchored_number_problems(text, facts)
    problems += pair_problems(text, facts)
    sheet = facts_text(facts)
    bad_clock = sorted({c for c in re.findall(r"\b\d{1,2}:\d\d\b", text) if c not in sheet})
    if bad_clock:
        problems.append("times not in the facts: " + ", ".join(bad_clock))

    ftext = (facts_text(facts) + " " + " ".join(n for names in (facts.get("_aliases") or {}).values() for n in names)).lower()
    unknown = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", f"{dek}\n{body}"):  # headlines are title case: numbers-checked only
        for m in NAME_WORD.finditer(sentence):
            w = m.group(0).rstrip(".").removesuffix("'s").removesuffix("’s")
            if w in ALLOWED_WORDS or (m.start() < 3 and w in SENTENCE_STARTERS) or w.lower() in COMMON_WORDS:
                continue
            if not re.search(r"\b" + re.escape(w.lower()) + r"\b", ftext):
                unknown.add(w)
    if unknown:
        problems.append("names or words not in the facts (remove them or use the names given): "
                        + ", ".join(sorted(unknown)[:12]))

    if kind == "recap":
        final = next((ln for ln in facts["game"] if ln.startswith("Final score: ")), None)
        m = final and re.match(r"Final score: .+? (\d+), .+? (\d+)", final)
        if m and f"{m.group(1)}-{m.group(2)}" not in text.replace("–", "-"):
            problems.append(f"state the final score as {m.group(1)}-{m.group(2)}")
    return problems


def write(kind, facts, retries=3):
    """Draft, check and (once) revise. Returns (article, problems, attempts)."""
    lo, hi = WORDS[kind]
    messages = [{"role": "system", "content": STYLE},
                {"role": "user", "content": TASKS[kind].format(lo=lo, hi=hi) + "\n\n" + facts_text(facts)}]
    article, problems = None, ["no draft"]
    for attempt in range(retries + 1):
        article, raw = ask(messages)
        problems = check(kind, article, facts)
        if not problems:
            problems = verify(article, facts)
        if not problems:
            return article, [], attempt + 1
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Revise the article to fix these problems, keeping to the facts:\n- "
                                                 + "\n- ".join(problems) + "\nReturn the full JSON again."}]
    return article, problems, retries + 1


# ---------------------------------------------------------------------------------------------- storage


def writer_model():
    return OPENAI_MODELS["writer"] if PROVIDER == "openai" and OPENAI_KEY and not USAGE["fallbacks"] else MODEL


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()  # "résumés" -> "resumes"
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def save(conn, league, kind, game_id, article, facts, problems, attempts, publish_ok, season, week, key=None):
    status = "published" if publish_ok and not problems else "review"
    base = slugify(article.get("headline") if isinstance(article, dict) and article.get("headline") else f"{kind}-{game_id}")
    slug = f"{base}-{(game_id or key or datetime.now().strftime('%Y%m%d'))}"
    a = article if isinstance(article, dict) else {}
    conn.execute(
        """
        INSERT INTO articles (league, kind, game_id, topic_key, season, week, slug, headline, dek, body, facts, checks,
                              status, model, published_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'published' THEN now() END)
        ON CONFLICT (league, kind, topic_key) DO NOTHING
        """,
        (league, kind, game_id, key or game_id, season, week, slug, (a.get("headline") or f"Untitled {kind}")[:200],
         a.get("dek"), a.get("body") or "", json.dumps(facts, default=str),
         json.dumps({"problems": problems, "attempts": attempts}), status, writer_model(), status))
    return status, slug


# ---------------------------------------------------------------------------------------------- jobs


def due_recaps(conn, league, limit, game_id=None):
    if game_id:
        return game_rows(conn, "g.league = %s AND g.game_id = %s", (league, game_id))
    ranked = "" if league == "nfl" else "AND (g.home_rank IS NOT NULL OR g.away_rank IS NOT NULL)"
    return game_rows(conn, f"""g.league = %s AND g.completed AND g.home_score IS NOT NULL
        AND g.start_time > now() - interval '3 days' AND g.start_time < now() - interval '3 hours' {ranked}
        AND NOT EXISTS (SELECT 1 FROM articles x WHERE x.league = g.league AND x.kind = 'recap' AND x.game_id = g.game_id)
        ORDER BY (g.home_rank IS NULL AND g.away_rank IS NULL), g.start_time LIMIT %s""", (league, limit))


def due_previews(conn, league, limit, game_id=None):
    if game_id:
        return game_rows(conn, "g.league = %s AND g.game_id = %s", (league, game_id))
    ranked = "" if league == "nfl" else "AND (g.home_rank IS NOT NULL OR g.away_rank IS NOT NULL)"
    return game_rows(conn, f"""g.league = %s AND NOT g.completed AND g.start_time > now() + interval '1 hour'
        AND g.start_time < now() + interval '3 days' {ranked}
        AND NOT EXISTS (SELECT 1 FROM articles x WHERE x.league = g.league AND x.kind = 'preview' AND x.game_id = g.game_id)
        ORDER BY g.start_time LIMIT %s""", (league, limit))


def run_game(conn, client, kind, g, dry_run, show_facts=False):
    summary = client.get("summary", params={"event": g["game_id"]})
    if not dry_run:
        live.save_boxscore(conn, g["league"], g["game_id"], summary, bool(g["completed"]))
    if kind == "recap":
        plays = live.plays_from_summary(g["league"], g["game_id"], summary)
        if plays and not dry_run:
            live.save_plays(conn, plays)
        wp = [{"home_win_prob": p[13]} for p in plays]
        facts = recap_facts(conn, g, summary, wp)
    else:
        facts = preview_facts(conn, g, summary)
    t = time.time()
    article, problems, attempts = write(kind, facts)
    label = f"{kind} {g['league']} {g['game_id']} {g['away_abbr']}@{g['home_abbr']}"
    if dry_run:
        if show_facts:
            print(facts_text(facts))
        print(f"== {label} ({time.time() - t:.0f}s, {attempts} attempt(s)) problems={problems}")
        print(json.dumps(article, indent=1, ensure_ascii=False) if isinstance(article, dict) else article)
        return
    status, slug = save(conn, g["league"], kind, g["game_id"], article, facts, problems, attempts, AUTOPUBLISH,
                        g["season"], g["week"])
    print(f"[newsroom] {label}: {status} {slug} ({time.time() - t:.0f}s, {attempts} attempt(s)) {problems or ''}",
          flush=True)


def run_editorial(conn, league, dry_run, force=False):
    facts = editorial_facts(conn, league)
    key = f"power-{facts['season']}-w{facts['after week']}"
    if not force and rows(conn, "SELECT 1 FROM articles WHERE league = %s AND kind = 'editorial' AND topic_key = %s",
                          (league, key)):
        return
    article, problems, attempts = write("editorial", facts)
    if dry_run:
        print(f"== editorial {league} {key} problems={problems}")
        print(json.dumps(article, indent=1, ensure_ascii=False) if isinstance(article, dict) else article)
        return
    status, slug = save(conn, league, "editorial", None, article, facts, problems, attempts, False,
                        facts["season"], facts["after week"], key)
    print(f"[newsroom] editorial {league} {key}: {status} {slug} {problems or ''}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["recap", "preview", "ratings", "editorial"], action="append")
    ap.add_argument("--league", choices=["nfl", "cfb"], action="append")
    ap.add_argument("--game")
    ap.add_argument("--limit", type=int, default=int(os.getenv("NEWSROOM_LIMIT", "8")), help="articles per kind and league")
    ap.add_argument("--dry-run", action="store_true", help="print drafts; write nothing")
    ap.add_argument("--force", action="store_true", help="editorial: write even if this week's exists")
    ap.add_argument("--show-facts", action="store_true", help="dry run: print the fact sheet too")
    args = ap.parse_args()
    kinds = args.kind or ["recap", "preview", "ratings"]
    leagues = args.league or ["nfl", "cfb"]
    with connect() as conn:
        init_db(conn)
        if not args.dry_run and not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            print("[newsroom] another run is in progress; skipping", flush=True)
            return 0
        failures = 0
        for kind in kinds:
            for league in leagues:
                if kind in ("editorial", "ratings"):
                    # Tuesdays (after the weekend's games) unless run explicitly
                    if args.kind or datetime.now(EASTERN).weekday() == 1:
                        (run_ratings if kind == "ratings" else run_editorial)(conn, league, args.dry_run, args.force)
                    continue
                client = EspnClient(league, max_attempts=2, timeout=(5, 30))
                games = (due_recaps if kind == "recap" else due_previews)(conn, league, args.limit, args.game)
                for g in games:
                    try:
                        run_game(conn, client, kind, g, args.dry_run, args.show_facts)
                    except (requests.RequestException, KeyError, ValueError) as e:
                        failures += 1
                        print(f"[newsroom] {kind} {league} {g['game_id']} failed: {e!r}", flush=True)
        if USAGE["calls"]:
            print(f"[newsroom] OpenAI: {USAGE['calls']} calls, {USAGE['input_tokens']} in / {USAGE['output_tokens']} out "
                  f"tokens, {USAGE['fallbacks']} fallbacks", flush=True)
        return 1 if failures and failures >= 3 else 0


if __name__ == "__main__":
    sys.exit(main())
