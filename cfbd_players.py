"""CFB offseason movement from CollegeFootballData: transfer portal, recruits, head coaches.

    /player/portal         transfers (2021+, the portal era), with 247 transfer ratings
    /recruiting/players    high-school recruits with stars / rating / committed team
    /coaches               head coaches per season (hire dates, games coached)

Responses are cached under data/cfbd/ (current season always re-fetched). Portal entries
carry names only, so each is matched to an ESPN athlete id through the origin team's roster
from the season before (normalized name, then last name + position).

    python cfbd_players.py
    python cfbd_players.py --start 2026
"""

import argparse
import re
import unicodedata


from backfill import current_season
from cfbd_box import Client, cached
from cfbd_seasons import team_ids
from database import connect, init_db

PORTAL_FIRST = 2021
RECRUITS_FIRST = 2002
COACHES_FIRST = 2004
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm(name):
    """'D'Angelo Smith Jr.' -> 'dangelo smith'."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = SUFFIX.sub("", re.sub(r"[^a-z ]", "", text.replace("-", " ")))
    return " ".join(text.split())


def fetch(conn, start, end, pause):
    client = Client(pause)
    for season in range(start, end + 1):
        refresh = season >= current_season()
        if season >= PORTAL_FIRST:
            cached(client, "player/portal", {"year": season}, refresh)
        if season >= RECRUITS_FIRST:
            cached(client, "recruiting/players", {"year": season, "classification": "HighSchool"}, refresh)
        if season >= COACHES_FIRST:
            cached(client, "coaches", {"year": season}, refresh)
    print(f"[cfbd_players] {client.calls} API calls", flush=True)


def rosters_by_team_season(conn):
    """(season, team_id) -> {normalized name: [(player_id, position)]}."""
    out = {}
    for season, team_id, player_id, name, position in conn.execute(
        "SELECT season, team_id, player_id, name, position FROM rosters WHERE league = 'cfb'"
    ):
        out.setdefault((season, team_id), {}).setdefault(norm(name), []).append((player_id, position))
    return out


def match_player(rosters, season, team_id, name, position):
    team = rosters.get((season - 1, team_id)) or {}
    hits = team.get(norm(name)) or []
    if len(hits) == 1:
        return hits[0][0]
    if len(hits) > 1:  # same name twice on a roster: use position
        same_pos = [pid for pid, pos in hits if pos == position]
        return same_pos[0] if len(same_pos) == 1 else None
    last = norm(name).split(" ")[-1:]  # nickname / first-name spelling differences
    candidates = [pid for key, rows in team.items() if key.split(" ")[-1:] == last
                  for pid, pos in rows if pos == position]
    return candidates[0] if len(candidates) == 1 else None


def load(conn, start, end):
    lookup = team_ids(conn)
    rosters = rosters_by_team_season(conn)

    transfers, matched = {}, 0
    for season in range(max(start, PORTAL_FIRST), end + 1):
        for t in cached(None, "player/portal", {"year": season}, False) or []:
            if t.get("eligibility") == "Withdrawn":
                continue
            name = " ".join(x for x in (t.get("firstName"), t.get("lastName")) if x).strip()
            origin = lookup(season - 1, t.get("origin")) or lookup(season, t.get("origin"))
            dest = lookup(season, t.get("destination")) if t.get("destination") else None
            player_id = match_player(rosters, season, origin, name, t.get("position")) if origin else None
            matched += player_id is not None
            key = f"{season}:{norm(name)}:{t.get('origin')}:{(t.get('transferDate') or '')[:10]}"
            transfers[key] = ("cfb", season, key, name, t.get("position"), origin, t.get("origin"), dest,
                              t.get("destination"), t.get("stars"), t.get("rating"), player_id)

    recruits = {}
    for season in range(max(start, RECRUITS_FIRST), end + 1):
        for r in cached(None, "recruiting/players", {"year": season, "classification": "HighSchool"}, False) or []:
            team_id = lookup(season, r.get("committedTo")) if r.get("committedTo") else None
            recruits[str(r["id"])] = ("cfb", season, str(r["id"]), r.get("athleteId"), r.get("name") or "Unknown",
                                      r.get("position"), team_id, r.get("stars"), r.get("rating"), r.get("ranking"))

    coaches = {}
    for season in range(max(start, COACHES_FIRST), end + 1):
        for c in cached(None, "coaches", {"year": season}, False) or []:
            name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
            for s in c.get("seasons") or []:
                if s.get("year") != season or s.get("teamId") is None:
                    continue
                key = (season, str(s["teamId"]))
                if key not in coaches or (s.get("games") or 0) > (coaches[key][5] or 0):
                    coaches[key] = ("cfb", season, str(s["teamId"]), name, None, s.get("games"),
                                    (c.get("hireDate") or "")[:10] or None)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM transfers WHERE league = 'cfb' AND season BETWEEN %s AND %s", (start, end))
        cur.execute("DELETE FROM recruits WHERE league = 'cfb' AND season BETWEEN %s AND %s", (start, end))
        cur.execute("DELETE FROM head_coaches WHERE league = 'cfb' AND season BETWEEN %s AND %s", (start, end))
        cur.executemany("INSERT INTO transfers (league, season, transfer_key, name, position, origin_team_id, "
                        "origin_name, dest_team_id, dest_name, stars, rating, player_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", list(transfers.values()))
        cur.executemany("INSERT INTO recruits (league, season, recruit_id, player_id, name, position, team_id, stars, "
                        "rating, ranking) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", list(recruits.values()))
        cur.executemany("INSERT INTO head_coaches (league, season, team_id, coach, games, hire_date) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", [c[:4] + c[5:] for c in coaches.values()])
    print(f"[cfbd_players] {len(transfers)} transfers ({matched} matched to a player), {len(recruits)} recruits, "
          f"{len(coaches)} head-coach seasons", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load CFB transfers, recruits and head coaches from CFBD.")
    parser.add_argument("--start", type=int, default=RECRUITS_FIRST)
    parser.add_argument("--end", type=int, default=current_season())
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--no-fetch", action="store_true", help="rebuild tables from cached responses only")
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        if not args.no_fetch:
            fetch(conn, args.start, args.end, args.pause)
        load(conn, args.start, args.end)


if __name__ == "__main__":
    main()
