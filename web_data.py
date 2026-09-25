"""Data assembly for the web pages: records, standings, season stats and leaderboards.

Everything reads tables the pipeline writes (games, team_affiliations, rosters, polls,
player_game_stats / team_game_stats with source 'box' for box scores, and the play-by-play
EPA sources). Season aggregates are cached in memory for a few minutes.
"""

import time
from collections import defaultdict

from database import connect

CACHE_SECONDS = 300
_cache = {}

EPA_SOURCE = {"nfl": "nflverse_pbp", "cfb": "espn_pbp"}
# Season stat totals: NFL counts the regular season only; college stats include bowls/playoffs.
STAT_SEASON_TYPES = {"nfl": [2], "cfb": [2, 3]}
POSITION_GROUPS = {
    "Offense": {"QB", "RB", "FB", "HB", "WR", "TE", "OL", "OT", "OG", "G", "T", "C", "IOL"},
    "Defense": {"DL", "DE", "DT", "NT", "EDGE", "LB", "ILB", "OLB", "MLB", "DB", "CB", "S", "FS", "SS", "SAF"},
    "Special teams": {"K", "P", "LS", "PK"},
}


def cached(key, build):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = build()
    _cache[key] = (time.time(), value)
    return value


def query(sql, params=()):
    with connect() as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def seasons(league):
    return cached(("seasons", league), lambda: [r["season"] for r in query(
        "SELECT DISTINCT season FROM games WHERE league = %s ORDER BY season DESC", (league,))])


def resolve_season(league, value):
    available = seasons(league)
    return value if value in available else available[0]


def teams(league):
    return cached(("teams", league), lambda: {r["team_id"]: r for r in query(
        "SELECT team_id, abbreviation, display_name, short_name, location, name, logo, color FROM teams "
        "WHERE league = %s", (league,))})


def affiliations(league, season):
    def build():
        rows = query("SELECT team_id, conference, division, classification FROM team_affiliations "
                     "WHERE league = %s AND season = %s", (league, season))
        if not rows:  # fall back to the latest season that has them
            rows = query("SELECT DISTINCT ON (team_id) team_id, conference, division, classification "
                         "FROM team_affiliations WHERE league = %s ORDER BY team_id, season DESC", (league,))
        return {r["team_id"]: r for r in rows}
    return cached(("aff", league, season), build)


def is_major(league, team_id, season):
    """Teams shown in directories and standings: all NFL teams, FBS teams in CFB."""
    if league == "nfl":
        return True
    aff = affiliations(league, season).get(team_id)
    return bool(aff and aff["classification"] == "fbs")


def season_games(league, season):
    return cached(("games", league, season), lambda: query(
        "SELECT game_id, season_type, week, start_time, completed, neutral_site, conference_game, notes, "
        "home_team_id, away_team_id, home_score, away_score, home_rank, away_rank FROM games "
        "WHERE league = %s AND season = %s ORDER BY start_time, game_id", (league, season)))


def _blank_record():
    return {"w": 0, "l": 0, "t": 0, "cw": 0, "cl": 0, "ct": 0, "dw": 0, "dl": 0, "dt": 0,
            "pf": 0, "pa": 0, "games": 0, "results": []}


def records(league, season):
    """Per team: overall, conference (CFB conference games / NFL same conference) and NFL division
    records, points for/against, and results in order (for streaks). NFL overall is the regular
    season; CFB includes bowls and playoffs."""
    def build():
        aff = affiliations(league, season)
        out = defaultdict(_blank_record)
        for g in season_games(league, season):
            if not g["completed"] or g["home_score"] is None:
                continue
            if league == "nfl" and g["season_type"] != 2:
                continue
            home, away = g["home_team_id"], g["away_team_id"]
            ha, aa = aff.get(home) or {}, aff.get(away) or {}
            if league == "cfb":
                in_conf = bool(g["conference_game"])
                in_div = False
            else:
                in_conf = ha.get("conference") and ha.get("conference") == aa.get("conference")
                in_div = ha.get("division") and ha.get("division") == aa.get("division")
            for team, pf, pa in ((home, g["home_score"], g["away_score"]), (away, g["away_score"], g["home_score"])):
                r = out[team]
                result = "w" if pf > pa else "l" if pf < pa else "t"
                r[result] += 1
                if in_conf:
                    r["c" + result] += 1
                if in_div:
                    r["d" + result] += 1
                r["pf"] += pf
                r["pa"] += pa
                r["games"] += 1
                r["results"].append(result)
        for r in out.values():
            r["overall"] = fmt_record(r["w"], r["l"], r["t"])
            r["conf"] = fmt_record(r["cw"], r["cl"], r["ct"])
            r["div"] = fmt_record(r["dw"], r["dl"], r["dt"])
            r["pct"] = win_pct(r["w"], r["l"], r["t"])
            r["conf_pct"] = win_pct(r["cw"], r["cl"], r["ct"])
            r["div_pct"] = win_pct(r["dw"], r["dl"], r["dt"])
            r["streak"] = streak(r["results"])
        return dict(out)
    return cached(("records", league, season), build)


def fmt_record(w, l, t):
    return f"{w}-{l}" + (f"-{t}" if t else "")


def win_pct(w, l, t):
    n = w + l + t
    return (w + 0.5 * t) / n if n else 0.0


def streak(results):
    if not results:
        return ""
    last, n = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        n += 1
    return f"{last.upper()}{n}"


def standings(league, season):
    """Groups of teams: NFL divisions, CFB (FBS) conferences, sorted like a standings table."""
    aff, recs = affiliations(league, season), records(league, season)
    groups = defaultdict(list)
    for team_id, a in aff.items():
        if not is_major(league, team_id, season):
            continue
        name = a["division"] if league == "nfl" else (a["conference"] or "Independent")
        rec = recs.get(team_id) or _blank_record() | {"overall": "0-0", "conf": "0-0", "div": "0-0", "pct": 0,
                                                       "conf_pct": 0, "div_pct": 0, "streak": ""}
        groups[name].append({"team_id": team_id, "team": teams(league).get(team_id), **rec})
    for name, rows in groups.items():
        if league == "nfl":
            rows.sort(key=lambda r: (-r["pct"], -r["div_pct"], -r["conf_pct"], -(r["pf"] - r["pa"])))
        elif name == "FBS Independents":
            rows.sort(key=lambda r: (-r["pct"], -(r["pf"] - r["pa"])))
        else:
            rows.sort(key=lambda r: (-r["conf_pct"], -r["cw"], -r["pct"], -(r["pf"] - r["pa"])))
    order = sorted(groups, key=lambda n: (n == "FBS Independents", n))
    return [{"name": n, "teams": groups[n]} for n in order]


def rosters(league, season):
    return cached(("rosters", league, season), lambda: query(
        "SELECT team_id, player_id, name, position, jersey, height, weight, experience, origin, headshot "
        "FROM rosters WHERE league = %s AND season = %s", (league, season)))


def player_seasons(league, season):
    """Season totals per player from box scores: {player_id: {name, team_id, position, games, stats}}.
    Regular season plus postseason."""
    def build():
        rows = query(
            "SELECT s.player_id, s.player_name, s.team_id, s.stats, g.start_time FROM player_game_stats s "
            "JOIN games g USING (league, game_id) WHERE s.league = %s AND s.source = 'box' AND g.season = %s "
            "AND g.season_type = ANY(%s) ORDER BY g.start_time",
            (league, season, STAT_SEASON_TYPES[league]))
        positions = {r["player_id"]: r["position"] for r in rosters(league, season)}
        out = {}
        for r in rows:
            p = out.setdefault(r["player_id"], {"player_id": r["player_id"], "name": r["player_name"], "games": 0,
                                               "stats": defaultdict(float), "position": positions.get(r["player_id"])})
            p["team_id"] = r["team_id"]  # latest team if traded / transferred mid-season
            p["games"] += 1
            for k, v in r["stats"].items():
                if isinstance(v, (int, float)):
                    if k.endswith("_long"):
                        p["stats"][k] = max(p["stats"][k], v)
                    elif k != "qbr":
                        p["stats"][k] += v
        for qb in query(
            "SELECT s.player_id, sum((s.stats->>'dropbacks')::float) db, sum((s.stats->>'epa_sum')::float) epa, "
            "min(s.player_name) name, (array_agg(s.team_id ORDER BY g.start_time DESC))[1] team_id "
            "FROM player_game_stats s JOIN games g USING (league, game_id) "
            "WHERE s.league = %s AND s.source = %s AND g.season = %s AND g.season_type = ANY(%s) GROUP BY 1",
            (league, EPA_SOURCE[league], season, STAT_SEASON_TYPES[league]),
        ):
            target = out.get(qb["player_id"])
            if target is None and league == "cfb":
                continue  # CFB play-by-play QBs are keyed by name, not athlete id; shown on their own board
            if target is not None:
                target["stats"]["dropbacks"] = qb["db"]
                target["stats"]["qb_epa"] = qb["epa"]
        for p in out.values():
            s = p["stats"]
            s["tackles"] = s.get("tackles", 0.0)
            if s.get("pass_att"):
                s["cmp_pct"] = 100 * s.get("pass_cmp", 0) / s["pass_att"]
                s["ypa"] = s.get("pass_yds", 0) / s["pass_att"]
            if s.get("rush_att"):
                s["ypc"] = s.get("rush_yds", 0) / s["rush_att"]
            if s.get("rec"):
                s["ypr"] = s.get("rec_yds", 0) / s["rec"]
            if s.get("dropbacks"):
                s["epa_per_db"] = s["qb_epa"] / s["dropbacks"]
            p["stats"] = dict(s)
        return out
    return cached(("players", league, season), build)


def qb_epa_board(league, season):
    """QB EPA per dropback from our play-by-play (CFB QBs keyed by team + name)."""
    return cached(("qbepa", league, season), lambda: query(
        "SELECT s.player_id, min(s.player_name) AS name, (array_agg(s.team_id ORDER BY g.start_time DESC))[1] AS team_id, "
        "sum((s.stats->>'dropbacks')::float) AS dropbacks, sum((s.stats->>'epa_sum')::float) AS epa, "
        "count(*) AS games FROM player_game_stats s JOIN games g USING (league, game_id) "
        "WHERE s.league = %s AND s.source = %s AND g.season = %s AND g.season_type = ANY(%s) GROUP BY 1",
        (league, EPA_SOURCE[league], season, STAT_SEASON_TYPES[league])))


def team_seasons(league, season):
    """Per team: games, points for/against per game, box-score averages for and against,
    turnover margin and play-by-play EPA per play (offense and defense)."""
    def build():
        box = defaultdict(dict)
        for r in query("SELECT s.game_id, s.team_id, s.stats FROM team_game_stats s JOIN games g USING (league, game_id) "
                       "WHERE s.league = %s AND s.source = 'box' AND g.season = %s AND g.season_type = ANY(%s)",
                       (league, season, STAT_SEASON_TYPES[league])):
            box[r["game_id"]][r["team_id"]] = r["stats"]
        epa = defaultdict(dict)
        for r in query("SELECT s.game_id, s.team_id, s.stats FROM team_game_stats s JOIN games g USING (league, game_id) "
                       "WHERE s.league = %s AND s.source = %s AND g.season = %s AND g.season_type = ANY(%s)",
                       (league, EPA_SOURCE[league], season, STAT_SEASON_TYPES[league])):
            epa[r["game_id"]][r["team_id"]] = r["stats"]
        out = defaultdict(lambda: {"games": 0, "pf": 0.0, "pa": 0.0, "for": defaultdict(float),
                                   "against": defaultdict(float), "box_games": 0, "off_epa": [], "def_epa": [],
                                   "third": [0.0, 0.0]})
        for g in season_games(league, season):
            if not g["completed"] or g["home_score"] is None or g["season_type"] not in STAT_SEASON_TYPES[league]:
                continue
            for team, opp, pf, pa in ((g["home_team_id"], g["away_team_id"], g["home_score"], g["away_score"]),
                                      (g["away_team_id"], g["home_team_id"], g["away_score"], g["home_score"])):
                t = out[team]
                t["games"] += 1
                t["pf"] += pf
                t["pa"] += pa
                mine, theirs = box[g["game_id"]].get(team), box[g["game_id"]].get(opp)
                if mine and theirs:
                    t["box_games"] += 1
                    for k, v in mine.items():
                        if isinstance(v, (int, float)):  # some older box scores have blank stats
                            t["for"][k] += v
                    for k, v in theirs.items():
                        if isinstance(v, (int, float)):
                            t["against"][k] += v
                    if mine.get("third_att"):
                        t["third"][0] += mine.get("third_conv", 0)
                        t["third"][1] += mine["third_att"]
                e = epa[g["game_id"]].get(team)
                if e and e.get("off_epa") is not None:
                    t["off_epa"].append((e["off_epa"], e.get("off_plays") or 1))
                    t["def_epa"].append((e["def_epa"], e.get("def_plays") or 1))
        result = {}
        for team_id, t in out.items():
            n, nb = t["games"], t["box_games"] or 1
            weighted = lambda pairs: sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else None  # noqa: E731
            f, a = t["for"], t["against"]
            result[team_id] = {
                "team_id": team_id, "games": n,
                "ppg": t["pf"] / n, "papg": t["pa"] / n,
                "ypg": f["total_yds"] / nb if t["box_games"] else None,
                "yapg": a["total_yds"] / nb if t["box_games"] else None,
                "pass_ypg": f["pass_yds"] / nb if t["box_games"] else None,
                "rush_ypg": f["rush_yds"] / nb if t["box_games"] else None,
                "pass_yapg": a["pass_yds"] / nb if t["box_games"] else None,
                "rush_yapg": a["rush_yds"] / nb if t["box_games"] else None,
                "to_margin": (a["turnovers"] - f["turnovers"]) if t["box_games"] else None,
                "sacks": f["sacks"] if t["box_games"] else None,
                "third_pct": 100 * t["third"][0] / t["third"][1] if t["third"][1] else None,
                "off_epa": weighted(t["off_epa"]), "def_epa": weighted(t["def_epa"]),
            }
        return result
    return cached(("teamseasons", league, season), build)


# (slug, title, stat, format, sort descending?, qualifier(player season) -> bool)
def _min(stat, per_team_game):
    """Qualifier like the official leaderboards: a minimum per game the player's team played."""
    return lambda p, team_games: p["stats"].get(stat, 0) >= per_team_game * max(team_games, 1)


PLAYER_LEADERS = {
    "offense": [
        ("passing-yards", "Passing yards", "pass_yds", "{:,.0f}", True, None),
        ("passing-touchdowns", "Passing TDs", "pass_td", "{:.0f}", True, None),
        ("yards-per-attempt", "Yards per attempt", "ypa", "{:.1f}", True, _min("pass_att", 14)),
        ("completion-percentage", "Completion %", "cmp_pct", "{:.1f}", True, _min("pass_att", 14)),
        ("rushing-yards", "Rushing yards", "rush_yds", "{:,.0f}", True, None),
        ("rushing-touchdowns", "Rushing TDs", "rush_td", "{:.0f}", True, None),
        ("yards-per-carry", "Yards per carry", "ypc", "{:.1f}", True, _min("rush_att", 6.25)),
        ("receiving-yards", "Receiving yards", "rec_yds", "{:,.0f}", True, None),
        ("receptions", "Receptions", "rec", "{:.0f}", True, None),
        ("receiving-touchdowns", "Receiving TDs", "rec_td", "{:.0f}", True, None),
    ],
    "defense": [
        ("tackles", "Tackles", "tackles", "{:.0f}", True, None),
        ("sacks", "Sacks", "sacks", "{:.1f}", True, None),
        ("tackles-for-loss", "Tackles for loss", "tfl", "{:.1f}", True, None),
        ("interceptions", "Interceptions", "def_int", "{:.0f}", True, None),
        ("passes-defended", "Passes defended", "pass_def", "{:.0f}", True, None),
    ],
    "special teams": [
        ("field-goals", "Field goals made", "fgm", "{:.0f}", True, None),
    ],
}
TEAM_LEADERS = {
    "offense": [
        ("points-per-game", "Points per game", "ppg", "{:.1f}", True),
        ("yards-per-game", "Yards per game", "ypg", "{:.1f}", True),
        ("passing-yards-per-game", "Passing yards per game", "pass_ypg", "{:.1f}", True),
        ("rushing-yards-per-game", "Rushing yards per game", "rush_ypg", "{:.1f}", True),
        ("offense-epa", "Offense EPA per play", "off_epa", "{:+.3f}", True),
        ("third-down-percentage", "Third down %", "third_pct", "{:.1f}", True),
    ],
    "defense": [
        ("points-allowed", "Points allowed per game", "papg", "{:.1f}", False),
        ("yards-allowed", "Yards allowed per game", "yapg", "{:.1f}", False),
        ("passing-yards-allowed", "Passing yards allowed per game", "pass_yapg", "{:.1f}", False),
        ("rushing-yards-allowed", "Rushing yards allowed per game", "rush_yapg", "{:.1f}", False),
        ("defense-epa", "Defense EPA per play allowed", "def_epa", "{:+.3f}", False),
        ("turnover-margin", "Turnover margin", "to_margin", "{:+.0f}", True),
        ("sacks", "Sacks", "sacks", "{:.0f}", True),
    ],
}
QB_EPA_SLUG = "qb-epa-per-dropback"


def _in_scope(league, season, team_id, conference):
    if league == "cfb" and not is_major(league, team_id, season):
        return False
    if conference:
        aff = affiliations(league, season).get(team_id) or {}
        return conference in (aff.get("conference"), aff.get("division"))
    return True


def player_board(league, season, spec, conference=None, limit=10):
    slug, title, stat, fmt, desc, qualifies = spec
    team_games = {t: v["games"] for t, v in team_seasons(league, season).items()}
    rows = [p for p in player_seasons(league, season).values()
            if p["stats"].get(stat) is not None and _in_scope(league, season, p["team_id"], conference)
            and (qualifies is None or qualifies(p, team_games.get(p["team_id"], p["games"])))]
    rows = [p for p in rows if p["stats"][stat] > 0 or not desc]
    rows.sort(key=lambda p: p["stats"][stat], reverse=desc)
    team_map = teams(league)
    return {"slug": slug, "title": title, "rows": [
        {"name": p["name"], "position": p["position"], "team": team_map.get(p["team_id"]), "team_id": p["team_id"],
         "games": p["games"], "value": fmt.format(p["stats"][stat])} for p in rows[:limit]]}


def qb_epa_leaders(league, season, conference=None, limit=10):
    min_per_team_game = 15
    team_games = {t: v["games"] for t, v in team_seasons(league, season).items()}
    rows = [r for r in qb_epa_board(league, season)
            if r["dropbacks"] and r["dropbacks"] >= min_per_team_game * team_games.get(r["team_id"], r["games"])
            and _in_scope(league, season, r["team_id"], conference)]
    rows.sort(key=lambda r: r["epa"] / r["dropbacks"], reverse=True)
    team_map = teams(league)
    return {"slug": QB_EPA_SLUG, "title": "QB EPA per dropback", "rows": [
        {"name": r["name"], "position": "QB", "team": team_map.get(r["team_id"]), "team_id": r["team_id"],
         "games": r["games"], "value": f"{r['epa'] / r['dropbacks']:+.3f}"} for r in rows[:limit]]}


def team_board(league, season, spec, conference=None, limit=10):
    slug, title, stat, fmt, desc = spec
    rows = [t for t in team_seasons(league, season).values()
            if t.get(stat) is not None and _in_scope(league, season, t["team_id"], conference)]
    rows.sort(key=lambda t: t[stat], reverse=desc)
    team_map = teams(league)
    return {"slug": slug, "title": title, "rows": [
        {"team": team_map.get(t["team_id"]), "team_id": t["team_id"], "games": t["games"],
         "value": fmt.format(t[stat])} for t in rows[:limit]]}


def find_player_spec(slug):
    for group in PLAYER_LEADERS.values():
        for spec in group:
            if spec[0] == slug:
                return spec
    return None


def find_team_spec(slug):
    for group in TEAM_LEADERS.values():
        for spec in group:
            if spec[0] == slug:
                return spec
    return None


def team_player_stats(league, season, team_id):
    """Season stat tables for one team's players, by category."""
    players = [p for p in player_seasons(league, season).values() if p["team_id"] == team_id]

    def table(key, cols):
        rows = [p for p in players if p["stats"].get(key)]
        rows.sort(key=lambda p: -p["stats"][key])
        return [{"name": p["name"], "position": p["position"], "games": p["games"],
                 **{c: p["stats"].get(c) for c in cols}} for p in rows]

    return {
        "Passing": ("pass_yds", ["pass_cmp", "pass_att", "cmp_pct", "pass_yds", "ypa", "pass_td", "pass_int", "epa_per_db"],
                    table("pass_att", ["pass_cmp", "pass_att", "cmp_pct", "pass_yds", "ypa", "pass_td", "pass_int",
                                       "epa_per_db"])),
        "Rushing": ("rush_yds", ["rush_att", "rush_yds", "ypc", "rush_td", "rush_long"],
                    table("rush_att", ["rush_att", "rush_yds", "ypc", "rush_td", "rush_long"])),
        "Receiving": ("rec_yds", ["rec", "targets", "rec_yds", "ypr", "rec_td", "rec_long"],
                      table("rec", ["rec", "targets", "rec_yds", "ypr", "rec_td", "rec_long"])),
        "Defense": ("tackles", ["tackles", "solo", "tfl", "sacks", "def_int", "pass_def"],
                    table("tackles", ["tackles", "solo", "tfl", "sacks", "def_int", "pass_def"])),
        "Kicking": ("fgm", ["fgm", "fga", "fg_long", "xpm", "xpa"], table("fga", ["fgm", "fga", "fg_long", "xpm", "xpa"])),
    }


STAT_LABELS = {
    "pass_cmp": "Cmp", "pass_att": "Att", "cmp_pct": "Pct", "pass_yds": "Yds", "ypa": "Y/A", "pass_td": "TD",
    "pass_int": "Int", "epa_per_db": "EPA/db", "rush_att": "Car", "rush_yds": "Yds", "ypc": "Avg", "rush_td": "TD",
    "rush_long": "Long", "rec": "Rec", "targets": "Tgt", "rec_yds": "Yds", "ypr": "Avg", "rec_td": "TD",
    "rec_long": "Long", "tackles": "Tkl", "solo": "Solo", "tfl": "TFL", "sacks": "Sack", "def_int": "Int",
    "pass_def": "PD", "fgm": "FGM", "fga": "FGA", "fg_long": "Long", "xpm": "XPM", "xpa": "XPA",
}
DECIMALS = {"cmp_pct": 1, "ypa": 1, "ypc": 1, "ypr": 1, "sacks": 1, "tfl": 1, "epa_per_db": 3}


def team_leaders(stat_tables):
    """Top player in the headline categories, for the team home tab."""
    picks = [("Passing", "pass_yds", "passing yards"), ("Rushing", "rush_yds", "rushing yards"),
             ("Receiving", "rec_yds", "receiving yards"), ("Defense", "tackles", "tackles"),
             ("Defense", "sacks", "sacks")]
    out = []
    for table, stat, label in picks:
        rows = [r for r in stat_tables[table][2] if r.get(stat)]
        if rows:
            best = max(rows, key=lambda r: r[stat])
            out.append({"label": label, "name": best["name"], "position": best["position"],
                        "value": f"{best[stat]:,.1f}".rstrip("0").rstrip(".")})
    return out


def roster_groups(league, season, team_id):
    rows = [r for r in rosters(league, season) if r["team_id"] == team_id]
    groups = {name: [] for name in POSITION_GROUPS}
    other = []
    for r in rows:
        for name, positions in POSITION_GROUPS.items():
            if (r["position"] or "").upper() in positions:
                groups[name].append(r)
                break
        else:
            other.append(r)
    if other:
        groups["Other"] = other
    for rows in groups.values():
        rows.sort(key=lambda r: (r["jersey"] is None, r["jersey"] or 0, r["name"]))
    return {k: v for k, v in groups.items() if v}


def poll_weeks(league, season):
    return query("SELECT DISTINCT season_type, week FROM polls WHERE league = %s AND season = %s "
                 "ORDER BY season_type DESC, week DESC", (league, season))


def poll_tables(league, season, season_type, week):
    """Each poll for a week with record and movement from that poll's previous release."""
    recs = records(league, season)
    team_map = teams(league)
    out = []
    for poll, title in (("Playoff Committee Rankings", "CFP Rankings"), ("AP Top 25", "AP Top 25"),
                        ("Coaches Poll", "Coaches Poll")):
        rows = query("SELECT team_id, rank, points, first_place_votes FROM polls WHERE league = %s AND season = %s "
                     "AND season_type = %s AND week = %s AND poll = %s ORDER BY rank",
                     (league, season, season_type, week, poll))
        if not rows:
            continue
        prev = query("SELECT season_type, week FROM polls WHERE league = %s AND season = %s AND poll = %s "
                     "AND (season_type, week) < (%s, %s) GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC LIMIT 1",
                     (league, season, poll, season_type, week))
        previous = {}
        if prev:
            previous = {r["team_id"]: r["rank"] for r in query(
                "SELECT team_id, rank FROM polls WHERE league = %s AND season = %s AND season_type = %s AND week = %s "
                "AND poll = %s", (league, season, prev[0]["season_type"], prev[0]["week"], poll))}
        for r in rows:
            r["team"] = team_map.get(r["team_id"])
            r["record"] = (recs.get(r["team_id"]) or {}).get("overall")
            r["movement"] = previous[r["team_id"]] - r["rank"] if r["team_id"] in previous else None
            r["new"] = bool(previous) and r["team_id"] not in previous
        out.append({"title": title, "rows": rows})
    return out


def latest_ap_ranks(league, season):
    rows = query("SELECT team_id, rank FROM polls WHERE league = %s AND season = %s AND poll = 'AP Top 25' "
                 "AND (season_type, week) = (SELECT season_type, week FROM polls WHERE league = %s AND season = %s "
                 "AND poll = 'AP Top 25' ORDER BY season_type DESC, week DESC LIMIT 1)",
                 (league, season, league, season))
    return {r["team_id"]: r["rank"] for r in rows}


def power_ratings(league, season):
    """Each team's ratings going into its next game of the season (or after its last one):
    Elo, opponent-adjusted EPA, starting-QB rating and form, with an Elo rank."""
    def build():
        rows = query(
            """
            WITH team_games AS (
                SELECT gf.start_time, gf.completed, gf.features, g.home_team_id AS team, 'home' AS side
                FROM game_features gf JOIN games g USING (league, game_id)
                WHERE gf.league = %(league)s AND gf.season = %(season)s
                UNION ALL
                SELECT gf.start_time, gf.completed, gf.features, g.away_team_id, 'away'
                FROM game_features gf JOIN games g USING (league, game_id)
                WHERE gf.league = %(league)s AND gf.season = %(season)s
            )
            SELECT DISTINCT ON (team) team, side, features FROM team_games
            ORDER BY team, completed, CASE WHEN completed THEN -extract(epoch FROM start_time)
                                           ELSE extract(epoch FROM start_time) END
            """,
            {"league": league, "season": season},
        )
        out = {}
        for r in rows:
            f, side = r["features"], r["side"]
            if f.get(f"{side}_elo") is None:
                continue
            out[r["team"]] = {"team_id": r["team"], "elo": f.get(f"{side}_elo"), "off": f.get(f"{side}_ridge_off_epa"),
                              "def": f.get(f"{side}_ridge_def_epa"), "qb": f.get(f"{side}_qb_rating"),
                              "form": f.get(f"{side}_ewm_margin")}
        ranked = sorted((t for t in out.values() if is_major(league, t["team_id"], season)), key=lambda t: -t["elo"])
        for i, t in enumerate(ranked, 1):
            t["rank"] = i
        return out
    return cached(("power", league, season), build)


def preseason_table(league, season):
    """Preseason ratings with contribution breakdowns, ranked (FBS)."""
    def build():
        rows = query("SELECT team_id, rating, baseline, actual, contributions, features FROM team_preseason "
                     "WHERE league = %s AND season = %s", (league, season))
        team_map = teams(league)
        recs = records(league, season)
        for r in rows:
            r["team"] = team_map.get(r["team_id"])
            r["record"] = (recs.get(r["team_id"]) or {}).get("overall")
            r["change"] = r["rating"] - r["baseline"] if r["baseline"] is not None else None
            r["n_in"] = int(r["features"].get("n_transfers_in") or 0)
            r["n_out"] = int(r["features"].get("n_transfers_out") or 0)
            r["elite_prob"] = r["features"].get("elite_prob")
        rows.sort(key=lambda r: -r["rating"])
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows
    return cached(("preseason", league, season), build)


def preseason_seasons(league):
    return [r["season"] for r in query("SELECT DISTINCT season FROM team_preseason WHERE league = %s ORDER BY 1 DESC",
                                       (league,))]


def team_transfers(league, season, team_id):
    """Incoming and outgoing transfers for a team's season, with what incoming players produced."""
    rows = query(
        """
        SELECT t.name, t.position, t.stars, t.rating, t.origin_team_id, t.origin_name, t.dest_team_id, t.dest_name,
               t.player_id
        FROM transfers t WHERE t.league = %s AND t.season = %s AND (t.dest_team_id = %s OR t.origin_team_id = %s)
        """,
        (league, season, team_id, team_id),
    )
    prev = {p["player_id"]: p for p in player_seasons(league, season - 1).values()} if rows else {}
    team_map = teams(league)
    incoming, outgoing = [], []
    for r in rows:
        stats = (prev.get(r["player_id"]) or {}).get("stats") or {}
        r["last_season"] = ", ".join(x for x in (
            f"{stats['pass_yds']:,.0f} pass yds" if stats.get("pass_yds", 0) >= 300 else "",
            f"{stats['rush_yds']:,.0f} rush yds" if stats.get("rush_yds", 0) >= 150 else "",
            f"{stats['rec_yds']:,.0f} rec yds" if stats.get("rec_yds", 0) >= 150 else "",
            f"{stats['tackles']:.0f} tkl" if stats.get("tackles", 0) >= 20 else "",
            f"{stats.get('sacks', 0):.1f} sacks" if stats.get("sacks", 0) >= 2 else "",
        ) if x)
        if r["dest_team_id"] == team_id:
            r["other"] = team_map.get(r["origin_team_id"]) or {"display_name": r["origin_name"]}
            r["other_id"] = r["origin_team_id"]
            incoming.append(r)
        else:
            r["other"] = team_map.get(r["dest_team_id"]) or ({"display_name": r["dest_name"]} if r["dest_name"] else None)
            r["other_id"] = r["dest_team_id"]
            outgoing.append(r)
    key = lambda r: (-(r["rating"] or 0), r["name"])  # noqa: E731
    return sorted(incoming, key=key), sorted(outgoing, key=key)


def team_preseason(league, season, team_id):
    row = next((r for r in preseason_table(league, season) if r["team_id"] == team_id), None)
    return row


def _stat_line(stats):
    stats = stats or {}
    return ", ".join(x for x in (
        f"{stats['pass_yds']:,.0f} pass yds" if stats.get("pass_yds", 0) >= 300 else "",
        f"{stats['rush_yds']:,.0f} rush yds" if stats.get("rush_yds", 0) >= 150 else "",
        f"{stats['rec_yds']:,.0f} rec yds" if stats.get("rec_yds", 0) >= 150 else "",
        f"{stats['tackles']:.0f} tkl" if stats.get("tackles", 0) >= 20 else "",
        f"{stats.get('sacks', 0):.1f} sacks" if stats.get("sacks", 0) >= 2 else "",
        f"{stats.get('def_int', 0):.0f} INT" if stats.get("def_int", 0) >= 2 else "",
    ) if x)


def nfl_moves(season, team_id):
    """NFL offseason for one team: veterans who arrived (produced elsewhere last season), players who
    left (produced here last season, not on this season's roster), and the draft class."""
    last = player_seasons("nfl", season - 1)
    roster = {r["player_id"]: r for r in rosters("nfl", season)}
    team_map = teams("nfl")
    arrived, departed = [], []
    for pid, r in roster.items():
        prev = last.get(pid)
        if r["team_id"] == team_id and prev and prev["team_id"] != team_id:
            line = _stat_line(prev["stats"])
            if line:
                arrived.append({"name": r["name"], "position": r["position"], "other_id": prev["team_id"],
                                "other": team_map.get(prev["team_id"]), "last_season": line, "_v": _weight(prev)})
    for pid, prev in last.items():
        if prev["team_id"] != team_id:
            continue
        now = roster.get(pid)
        if now and now["team_id"] == team_id:
            continue
        line = _stat_line(prev["stats"])
        if line:
            departed.append({"name": prev["name"], "position": prev.get("position"),
                             "other_id": now["team_id"] if now else None,
                             "other": team_map.get(now["team_id"]) if now else None,
                             "last_season": line, "_v": _weight(prev)})
    draft = query("SELECT pick, round, name, position FROM draft_picks WHERE league = 'nfl' AND season = %s "
                  "AND team_id = %s ORDER BY pick", (season, team_id))
    key = lambda r: -r["_v"]  # noqa: E731
    return sorted(arrived, key=key), sorted(departed, key=key), draft


def _weight(p):
    """Rough production size for ordering lists (yards + 20 per tackle/sack)."""
    s = p["stats"]
    return (s.get("pass_yds", 0) * 0.5 + s.get("rush_yds", 0) + s.get("rec_yds", 0)
            + 20 * (s.get("tackles", 0) + 3 * s.get("sacks", 0) + 3 * s.get("def_int", 0)))


def game_availability(league, g):
    """Injury report for a game, by side: NFL official report lines; CFB news-based statuses
    (latest report per player within three weeks before kickoff, season-ending all season)."""
    teams = {"home": g["home_team_id"], "away": g["away_team_id"]}
    out = {"home": [], "away": []}
    if league == "nfl":
        rows = query("SELECT team_id, player_name, position, status, headline AS detail FROM player_status "
                     "WHERE league = 'nfl' AND source = 'nfl_injury_report' AND source_id = %s", (g["game_id"],))
        for r in rows:
            side = "home" if r["team_id"] == teams["home"] else "away"
            out[side].append(r)
    else:
        rows = query(
            """
            SELECT DISTINCT ON (team_id, lower(player_name)) team_id, player_name, position, status, games,
                   headline, url, published
            FROM player_status
            WHERE league = 'cfb' AND source = 'news_llm' AND team_id = ANY(%s) AND published < %s
              AND (published > %s - interval '21 days'
                   OR (status = 'season-ending' AND extract(year FROM published) = extract(year FROM %s::timestamptz)))
            ORDER BY team_id, lower(player_name), published DESC
            """,
            ([teams["home"], teams["away"]], g["start_time"], g["start_time"], g["start_time"]))
        for r in rows:
            if r["status"] in ("returning", "probable"):
                continue  # back / expected to play
            side = "home" if r["team_id"] == teams["home"] else "away"
            out[side].append(r)
    order = {"season-ending": 0, "out": 1, "suspended": 2, "doubtful": 3, "questionable": 4}
    for side in out:
        out[side].sort(key=lambda r: (order.get(r["status"], 9), r["player_name"]))
    return out


# --- Newsroom articles (newsroom.py writes; /newsroom reviews) --------------------------------------------------

KIND_LABELS = {"preview": "Preview", "recap": "Recap", "ratings": "Power Ratings", "editorial": "Column"}
KIND_PLURALS = {"preview": "Previews", "recap": "Recaps", "ratings": "Power Ratings", "editorial": "Columns"}
ARTICLE_COLS = ("id, league, kind, game_id, season, week, slug, headline, dek, status, model, created_at, "
                "published_at, updated_at")


def _label(rows):
    for r in rows:
        r["kind_label"] = KIND_LABELS.get(r["kind"], r["kind"].title())
    return rows


def latest_articles(league=None, n=6, kind=None, offset=0):
    """Published articles, newest first."""
    where, params = ["status = 'published'"], []
    if league:
        where.append("league = %s")
        params.append(league)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    return _label(query(f"SELECT {ARTICLE_COLS} FROM articles WHERE {' AND '.join(where)} "
                        "ORDER BY published_at DESC, id DESC LIMIT %s OFFSET %s", (*params, n, offset)))


def article(slug=None, article_id=None, published_only=True):
    rows = query(f"SELECT {ARTICLE_COLS}, body, facts, checks, reviewed_at FROM articles WHERE "
                 + ("slug = %s" if slug else "id = %s") + (" AND status = 'published'" if published_only else ""),
                 (slug or article_id,))
    return _label(rows)[0] if rows else None


def game_articles(league, game_id):
    """Published preview/recap for a game: {kind: article}."""
    return {r["kind"]: r for r in _label(query(
        f"SELECT {ARTICLE_COLS} FROM articles WHERE league = %s AND game_id = %s AND status = 'published'",
        (league, game_id)))}


def review_queue():
    return {status: _label(query(f"SELECT {ARTICLE_COLS}, checks FROM articles WHERE status = %s "
                                 "ORDER BY created_at DESC LIMIT %s", (status, 100 if status == "review" else 40)))
            for status in ("review", "published", "rejected")}


def update_article(article_id, status=None, headline=None, dek=None, body=None):
    """Review actions from /newsroom (the web role may only touch these columns)."""
    sets, params = ["updated_at = now()", "reviewed_at = now()"], []
    for col, val in (("headline", headline), ("dek", dek), ("body", body)):
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if status:
        sets.append("status = %s")
        params.append(status)
        if status == "published":
            sets.append("published_at = COALESCE(published_at, now())")
    with connect() as conn:
        conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = %s", (*params, article_id))


BOX_MAIN = ("passing", "rushing", "receiving")
BOX_MORE = ("defensive", "interceptions", "fumbles", "kickReturns", "puntReturns", "kicking", "punting")


def game_boxscore(league, game_id, away_id, home_id):
    """ESPN player box score arranged for side-by-side display: [(category name, title, away cat, home cat)],
    main categories first. None when we have no box score for the game."""
    rows = query("SELECT data, final, updated_at FROM game_boxscores WHERE league = %s AND game_id = %s",
                 (league, game_id))
    if not rows:
        return None
    by_team = {t["team_id"]: {c["name"]: c for c in t["categories"]} for t in rows[0]["data"]}
    away, home = by_team.get(str(away_id), {}), by_team.get(str(home_id), {})
    out = []
    for name in BOX_MAIN + BOX_MORE:
        a, h = away.get(name), home.get(name)
        if a or h:
            out.append({"name": name, "title": (a or h)["title"], "away": a, "home": h, "main": name in BOX_MAIN})
    return {"categories": out, "final": rows[0]["final"], "updated_at": rows[0]["updated_at"]} if out else None
