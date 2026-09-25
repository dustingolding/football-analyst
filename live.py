"""Live scores and play-by-play from ESPN, for the web app to show while games are on.

Runs continuously (a Kubernetes Deployment):
    every 30 s   current-week scoreboards for both leagues -> live_games
                 (score, clock, down & distance, possession, red zone, timeouts, last play,
                 ESPN's in-game win probability)
    every 60 s   play-by-play for games in progress -> live_plays (with per-play win probability)
When no game is live or about to start, it only checks every 5 minutes.

Final scores reach the games table through the regular refresh pipeline; until then the web
app shows them from live_games.

    python live.py            # run forever
    python live.py --once     # one pass (for testing)
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import requests
from psycopg.types.json import Jsonb

from database import connect, init_db
from espn_client import LEAGUES, EspnClient

SCORES_EVERY = 30
PLAYS_EVERY = 60
IDLE_EVERY = 300
SOON = timedelta(minutes=30)     # games starting within this window count as active
RECENT = timedelta(minutes=20)   # keep polling plays briefly after a final, to catch the last ones


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def scoreboard(client):
    """Current week's games (the scoreboard default), as live_games rows plus kickoff times."""
    params = dict(client.config["scoreboard_params"])
    rows, kickoffs = [], {}
    for event in client.get("scoreboard", params=params).get("events", []):
        comp = event["competitions"][0]
        status = comp.get("status") or event.get("status") or {}
        state = (status.get("type") or {}).get("state")
        if state not in ("pre", "in", "post"):
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        home, away = sides.get("home") or {}, sides.get("away") or {}
        sit = comp.get("situation") or {}
        last = sit.get("lastPlay") or {}
        prob = (last.get("probability") or {}).get("homeWinPercentage")
        if state == "post" and prob is None:
            winner_home = num(home.get("score")) is not None and num(home.get("score")) > num(away.get("score") or 0)
            prob = 1.0 if winner_home else 0.0
        broadcast = next((n for b in comp.get("broadcasts") or [] for n in b.get("names") or []), None)
        record = lambda side: next((r.get("summary") for r in side.get("records") or []), None)  # noqa: E731
        rows.append((
            client.league, event["id"], state, (status.get("type") or {}).get("shortDetail"),
            status.get("period"), status.get("displayClock"),
            num(home.get("score")) if state != "pre" else None, num(away.get("score")) if state != "pre" else None,
            sit.get("possession"), sit.get("downDistanceText"), sit.get("isRedZone"),
            sit.get("homeTimeouts"), sit.get("awayTimeouts"), prob, last.get("text"),
            broadcast, record(home), record(away),
        ))
        kickoffs[event["id"]] = (state, parse_time(event.get("date")))
    return rows, kickoffs


def save_scores(conn, rows):
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO live_games (league, game_id, state, detail, period, clock, home_score, away_score,
                                    possession_team_id, down_distance, red_zone, home_timeouts, away_timeouts,
                                    home_win_prob, last_play, broadcast, home_record, away_record, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (league, game_id) DO UPDATE SET
                state = EXCLUDED.state, detail = EXCLUDED.detail, period = EXCLUDED.period,
                clock = EXCLUDED.clock, home_score = EXCLUDED.home_score, away_score = EXCLUDED.away_score,
                possession_team_id = EXCLUDED.possession_team_id, down_distance = EXCLUDED.down_distance,
                red_zone = EXCLUDED.red_zone, home_timeouts = EXCLUDED.home_timeouts,
                away_timeouts = EXCLUDED.away_timeouts,
                home_win_prob = COALESCE(EXCLUDED.home_win_prob, live_games.home_win_prob),
                last_play = COALESCE(EXCLUDED.last_play, live_games.last_play),
                broadcast = COALESCE(EXCLUDED.broadcast, live_games.broadcast),
                home_record = EXCLUDED.home_record, away_record = EXCLUDED.away_record, updated_at = now()
            """,
            rows,
        )


def plays(client, game_id):
    """All plays so far, with ESPN's win probability after each."""
    return plays_from_summary(client.league, game_id, client.get("summary", params={"event": game_id}))


def plays_from_summary(league, game_id, summary):
    """live_plays rows from an ESPN game summary."""
    probability = {w.get("playId"): w.get("homeWinPercentage") for w in summary.get("winprobability") or []}
    drives = summary.get("drives") or {}
    all_drives = list(drives.get("previous") or [])
    current = drives.get("current")
    if current and all(d.get("id") != current.get("id") for d in all_drives):
        all_drives.append(current)
    rows = []
    for drive_number, drive in enumerate(all_drives):
        for p in drive.get("plays") or []:
            team = ((p.get("start") or {}).get("team") or {}).get("id") or ((drive.get("team") or {}).get("id"))
            rows.append((
                league, game_id, str(p["id"]), num(p.get("sequenceNumber")), drive_number,
                (p.get("period") or {}).get("number"), (p.get("clock") or {}).get("displayValue"), team,
                (p.get("type") or {}).get("text"), p.get("text"), num(p.get("homeScore")), num(p.get("awayScore")),
                bool(p.get("scoringPlay")), probability.get(str(p["id"])),
            ))
    return rows


BOX_TITLES = {"passing": "Passing", "rushing": "Rushing", "receiving": "Receiving", "fumbles": "Fumbles",
              "defensive": "Defense", "interceptions": "Interceptions", "kickReturns": "Kick returns",
              "puntReturns": "Punt returns", "kicking": "Kicking", "punting": "Punting"}


def boxscore_from_summary(summary):
    """Player box score from an ESPN game summary, in ESPN's own columns (both leagues use the same shape)."""
    teams = []
    for t in (summary.get("boxscore") or {}).get("players") or []:
        cats = []
        for c in t.get("statistics") or []:
            players = [{"id": str((a.get("athlete") or {}).get("id") or ""),
                        "name": (a.get("athlete") or {}).get("displayName"),
                        "stats": a.get("stats") or []}
                       for a in c.get("athletes") or [] if a.get("stats")]
            if players:
                cats.append({"name": c.get("name"), "title": BOX_TITLES.get(c.get("name"), c.get("text") or c.get("name")),
                             "labels": c.get("labels") or [], "players": players, "totals": c.get("totals") or []})
        if cats:
            teams.append({"team_id": str((t.get("team") or {}).get("id")), "categories": cats})
    return teams


def save_boxscore(conn, league, game_id, summary, final):
    teams = boxscore_from_summary(summary)
    if teams:
        conn.execute(
            "INSERT INTO game_boxscores (league, game_id, data, final) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (league, game_id) DO UPDATE SET data = EXCLUDED.data, final = EXCLUDED.final, updated_at = now()",
            (league, game_id, Jsonb(teams), final))
    return bool(teams)


def save_plays(conn, rows):
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO live_plays (league, game_id, play_id, sequence, drive, period, clock, team_id, play_type,
                                    text, home_score, away_score, scoring, home_win_prob)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (league, game_id, play_id) DO UPDATE SET
                sequence = EXCLUDED.sequence, drive = EXCLUDED.drive, period = EXCLUDED.period,
                clock = EXCLUDED.clock, team_id = EXCLUDED.team_id, play_type = EXCLUDED.play_type,
                text = EXCLUDED.text, home_score = EXCLUDED.home_score, away_score = EXCLUDED.away_score,
                scoring = EXCLUDED.scoring, home_win_prob = COALESCE(EXCLUDED.home_win_prob, live_plays.home_win_prob)
            """,
            rows,
        )


def run(once=False):
    clients = {league: EspnClient(league, max_attempts=2, timeout=(5, 20)) for league in LEAGUES}
    last_plays = {}        # (league, game_id) -> last play-by-play fetch time
    finished_at = {}       # (league, game_id) -> when we first saw it final
    with connect() as conn:
        init_db(conn)
        while True:
            now = datetime.now(timezone.utc)
            active = False
            for league, client in clients.items():
                try:
                    rows, kickoffs = scoreboard(client)
                except requests.RequestException as exc:
                    print(f"[live] {league} scoreboard failed: {exc}", flush=True)
                    continue
                save_scores(conn, rows)
                live = []
                for game_id, (state, kickoff) in kickoffs.items():
                    key = (league, game_id)
                    if state == "post":
                        finished_at.setdefault(key, now)
                    if state == "in" or (state == "pre" and kickoff and kickoff - now < SOON):
                        active = True
                    recently_final = state == "post" and now - finished_at[key] < RECENT and key in last_plays
                    if state == "in" or recently_final:
                        live.append(game_id)
                for game_id in live:
                    key = (league, game_id)
                    if now - last_plays.get(key, datetime.min.replace(tzinfo=timezone.utc)) < timedelta(seconds=PLAYS_EVERY - 5):
                        continue
                    try:
                        summary = client.get("summary", params={"event": game_id})
                        save_plays(conn, plays_from_summary(league, game_id, summary))
                        save_boxscore(conn, league, game_id, summary, kickoffs[game_id][0] == "post")
                        last_plays[key] = now
                    except requests.RequestException as exc:
                        print(f"[live] {league} {game_id} plays failed: {exc}", flush=True)
                in_progress = sum(1 for s, _ in kickoffs.values() if s == "in")
                print(f"[live] {league}: {len(rows)} games, {in_progress} in progress, {len(live)} play feeds",
                      flush=True)
            if once:
                return
            time.sleep(SCORES_EVERY if active else IDLE_EVERY)


def main():
    parser = argparse.ArgumentParser(description="Poll ESPN for live scores and play-by-play.")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    run(parser.parse_args().once)


if __name__ == "__main__":
    main()
