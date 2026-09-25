"""Rebuild the clean teams and games tables from raw_payloads.

Safe to re-run at any time (e.g. after each backfill): rows are upserted, and each game
takes its values from the most recently fetched payload that contains it.

    python etl.py
    python etl.py --league nfl
"""

import argparse
from datetime import datetime, timezone

from database import connect, init_db
from espn_client import LEAGUES

UNRANKED = 99
SKIPPED_GAME_TYPES = {"ALLSTAR"}  # NFL Pro Bowl
ALL_STAR_TEAMS = {"AFC", "NFC"}   # older Pro Bowls are typed as regular (STD) games

GAME_COLUMNS = [
    "league", "game_id", "season", "season_type", "week", "start_time", "status", "completed",
    "game_type", "notes", "neutral_site", "conference_game",
    "home_team_id", "away_team_id", "home_conference_id", "away_conference_id",
    "home_score", "away_score", "home_rank", "away_rank", "home_linescores", "away_linescores",
    "venue_id", "venue_name", "venue_city", "venue_state", "venue_indoor", "attendance",
]

TEAM_COLUMNS = [
    "league", "team_id", "abbreviation", "location", "name", "display_name", "short_name",
    "color", "alternate_color", "logo", "conference_id", "is_active",
]


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_real_team(team):
    return str(team.get("id", "")).isdigit()


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def team_fields(team):
    logo = team.get("logo") or next((l.get("href") for l in team.get("logos", [])), None)
    return {
        "abbreviation": team.get("abbreviation"),
        "location": team.get("location"),
        "name": team.get("name"),
        "display_name": team.get("displayName"),
        "short_name": team.get("shortDisplayName"),
        "color": team.get("color"),
        "alternate_color": team.get("alternateColor"),
        "logo": logo,
        "conference_id": team.get("conferenceId"),
        "is_active": team.get("isActive"),
    }


def parse_game(league, event):
    """Return a games row dict, or None for events that aren't real games."""
    if not event.get("id") or not event.get("competitions"):
        return None
    comp = event["competitions"][0]
    game_type = (comp.get("type") or {}).get("abbreviation")
    if game_type in SKIPPED_GAME_TYPES:
        return None

    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    home, away = sides.get("home"), sides.get("away")
    if not home or not away:
        return None
    # Unscheduled playoff/bowl slots use placeholder "TBD" teams with ids -1 and -2.
    if not (is_real_team(home["team"]) and is_real_team(away["team"])):
        return None
    if {home["team"].get("abbreviation"), away["team"].get("abbreviation")} & ALL_STAR_TEAMS:
        return None

    status = (event.get("status") or comp.get("status") or {}).get("type", {})
    completed = bool(status.get("completed"))
    has_score = completed or status.get("state") == "in"

    def rank(side):
        value = to_int((side.get("curatedRank") or {}).get("current"))
        return None if value in (None, 0, UNRANKED) else value

    def linescores(side):
        values = [to_int(l.get("value")) for l in side.get("linescores", [])]
        return values or None

    venue = comp.get("venue") or {}
    address = venue.get("address") or {}
    notes = comp.get("notes") or []

    return {
        "league": league,
        "game_id": event["id"],
        "season": event["season"]["year"],
        "season_type": event["season"]["type"],
        "week": (event.get("week") or {}).get("number"),
        "start_time": parse_time(event.get("date") or comp.get("date")),
        "status": status.get("name"),
        "completed": completed,
        "game_type": game_type,
        "notes": notes[0].get("headline") if notes else None,
        "neutral_site": comp.get("neutralSite"),
        "conference_game": comp.get("conferenceCompetition"),
        "home_team_id": home["team"]["id"],
        "away_team_id": away["team"]["id"],
        "home_conference_id": home["team"].get("conferenceId"),
        "away_conference_id": away["team"].get("conferenceId"),
        "home_score": to_int(home.get("score")) if has_score else None,
        "away_score": to_int(away.get("score")) if has_score else None,
        "home_rank": rank(home),
        "away_rank": rank(away),
        "home_linescores": linescores(home),
        "away_linescores": linescores(away),
        "venue_id": venue.get("id"),
        "venue_name": venue.get("fullName"),
        "venue_city": address.get("city"),
        "venue_state": address.get("state"),
        "venue_indoor": venue.get("indoor"),
        "attendance": to_int(comp.get("attendance")) or None,
    }


def upsert(conn, table, columns, key_columns, rows):
    if not rows:
        return
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in key_columns)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {updates}, updated_at = now()"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, [[row[c] for c in columns] for row in rows])


def load_league(conn, league):
    games = {}
    teams = {}  # team_id -> (start_time of the game the info came from, fields)
    skipped = 0

    # Oldest fetch first, so the newest copy of a game (e.g. a re-fetched current week) wins.
    cur = conn.execute(
        """
        SELECT payload->'events' FROM raw_payloads
        WHERE league = %s AND endpoint = 'scoreboard'
        ORDER BY fetched_at, id
        """,
        (league,),
    )
    for (events,) in cur:
        for event in events or []:
            game = parse_game(league, event)
            if game is None:
                skipped += 1
                continue
            games[game["game_id"]] = game

            # Keep team info from each team's most recent game, so conference reflects today.
            start = game["start_time"] or datetime.min.replace(tzinfo=timezone.utc)
            for side in event["competitions"][0]["competitors"]:
                team = side.get("team") or {}
                seen = teams.get(team.get("id"))
                if is_real_team(team) and (seen is None or start >= seen[0]):
                    teams[team["id"]] = (start, team_fields(team))

    # The teams endpoint has the current list (incl. teams with no stored games) and better logos.
    row = conn.execute(
        "SELECT payload FROM raw_payloads WHERE league = %s AND endpoint = 'teams' ORDER BY fetched_at DESC LIMIT 1",
        (league,),
    ).fetchone()
    if row:
        for entry in row[0]["sports"][0]["leagues"][0]["teams"]:
            if not is_real_team(entry["team"]):
                continue
            fields = team_fields(entry["team"])
            _, existing = teams.get(entry["team"]["id"], (None, {}))
            merged = {**fields, **existing, **{k: v for k, v in fields.items() if v is not None}}
            teams[entry["team"]["id"]] = (None, merged)

    team_rows = [{"league": league, "team_id": team_id, **fields} for team_id, (_, fields) in teams.items()]
    upsert(conn, "teams", TEAM_COLUMNS, ["league", "team_id"], team_rows)
    upsert(conn, "games", GAME_COLUMNS, ["league", "game_id"], list(games.values()))

    # The tables mirror raw_payloads, so drop rows the current parser no longer produces.
    conn.execute("DELETE FROM games WHERE league = %s AND NOT (game_id = ANY(%s))", (league, list(games)))
    conn.execute("DELETE FROM teams WHERE league = %s AND NOT (team_id = ANY(%s))", (league, list(teams)))

    completed = sum(g["completed"] for g in games.values())
    print(f"[{league}] {len(team_rows)} teams, {len(games)} games ({completed} completed), "
          f"{skipped} events skipped", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Build teams and games tables from raw_payloads.")
    parser.add_argument("--league", nargs="+", choices=list(LEAGUES), default=list(LEAGUES))
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        for league in args.league:
            with conn.transaction():
                load_league(conn, league)


if __name__ == "__main__":
    main()
