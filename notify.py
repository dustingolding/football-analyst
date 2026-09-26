"""Push alerts for followed teams, driven by the live scores live.py writes.

Runs continuously (a Kubernetes Deployment). Every 20 s it compares each game in live_games with the
last state it saw (push_game_state) and alerts the devices following either team (push_follows):
    kickoff   pre -> in
    score     a score went up (extra points and two-point tries are folded into the next alert)
    final     -> post
    upset     a pregame favorite (60%+) down to 35% or less from the second half on
    close     one-score game with two minutes or less in the 4th, or overtime
    news      a story about a followed team was published (newsroom articles, via article_teams)
It also keeps registered Live Activities (push_activities) in step with their games' live state, and starts
one (push-to-start) on devices that auto-follow a team whose game is live.
Each alert is inserted into push_events before it's sent, so a restart or retry never repeats one.
A game seen for the first time is recorded without alerts, so starting up mid-game or after
downtime doesn't replay the day, and changes older than STALE are recorded but not sent.

Needs APNS_KEY_P8, APNS_KEY_ID and APNS_TEAM_ID (the apns Secret). Without them it records events
and logs what it would have sent.

    python notify.py                      # run forever
    python notify.py --once               # one pass
    python notify.py --dry-run            # print the alerts a pass would send; writes nothing
    python notify.py --test <install_id>  # send a test alert to one registered device
"""

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from database import TEAM_STORY_SQL, connect, init_db
from push import Apns

EVERY = 20
STALE = timedelta(minutes=15)
PREFERENCE = {"kickoff": "alert_kickoff", "score": "alert_scoring", "final": "alert_final", "news": "alert_news",
              "upset": "alert_upset", "close": "alert_close"}
EXPIRES = {"kickoff": 30 * 60, "score": 30 * 60, "final": 6 * 3600, "news": 12 * 3600, "upset": 20 * 60,
           "close": 10 * 60, "test": 3600}
UPSET_FAVORITE = 0.60    # pregame win probability that makes a team the favorite for upset alerts
UPSET_TROUBLE = 0.35     # the favorite's live win probability, from the second half on, that triggers it
CLOSE_MARGIN = 8         # one score
CLOSE_SECONDS = 120
NEWS_WINDOW = timedelta(hours=2)  # only stories published this recently alert (no backlog blast on first run)
SITE = {"prod": "https://sidelinewire.com", "dev": "https://dev.sidelinewire.com"}.get(os.getenv("SITE_ENV", "prod"),
                                                                                      "https://sidelinewire.com")
NEWS_KIND = {"preview": "Preview", "recap": "Recap", "ratings": "Power Ratings", "editorial": "Column"}

GAMES = """
    SELECT l.league, l.game_id, l.state, l.detail, l.home_score, l.away_score, l.last_play, l.broadcast,
           l.updated_at, l.possession_team_id, l.down_distance, l.red_zone, l.home_win_prob, l.period, l.clock,
           g.home_team_id, g.away_team_id,
           (SELECT p.home_win_prob FROM predictions p WHERE p.league = l.league AND p.game_id = l.game_id
              AND p.home_win_prob IS NOT NULL
            ORDER BY CASE p.model WHEN 'xgb_market' THEN 0 WHEN 'elo' THEN 2 ELSE 1 END LIMIT 1) AS pregame_prob,
           s.state AS seen_state, s.home_score AS seen_home, s.away_score AS seen_away
    FROM live_games l
    JOIN games g USING (league, game_id)
    LEFT JOIN push_game_state s USING (league, game_id)
    WHERE l.updated_at > now() - interval '2 days'
"""


def team_names(conn):
    rows = conn.execute("SELECT league, team_id, abbreviation, short_name, display_name, color FROM teams").fetchall()
    return {(league, team_id): {"abbr": abbr or short or team_id, "short": short or display or abbr or team_id,
                                "color": color}
            for league, team_id, abbr, short, display, color in rows}


def scoring_label(points):
    return {3: "field goal", 2: "safety", 6: "touchdown", 7: "touchdown", 8: "touchdown"}.get(points, "score")


def detect(game, names):
    """Alerts for one game's change since the last pass, as (kind, detail, title, body)."""
    league = game["league"]
    home = names.get((league, game["home_team_id"]), {"abbr": game["home_team_id"], "short": game["home_team_id"]})
    away = names.get((league, game["away_team_id"]), {"abbr": game["away_team_id"], "short": game["away_team_id"]})
    h, a = game["home_score"], game["away_score"]
    line = f"{away['abbr']} {a} – {home['abbr']} {h}"
    events = []

    if game["seen_state"] == "pre" and game["state"] == "in":
        body = f"On {game['broadcast']}." if game["broadcast"] else "Underway now."
        events.append(("kickoff", "kickoff", f"Kickoff: {away['short']} at {home['short']}", body))

    if game["state"] == "post" and game["seen_state"] != "post":
        if h is not None and a is not None:
            overtime = " (OT)" if "OT" in (game["detail"] or "") else ""
            if h == a:
                body = f"{away['short']} and {home['short']} tie{overtime}."
            else:
                winner, loser = (home, away) if h > a else (away, home)
                body = f"{winner['short']} beat {loser['short']}{overtime}."
            events.append(("final", "final", f"Final: {line}", body))
    elif game["state"] in ("in", "post") and None not in (h, a, game["seen_home"], game["seen_away"]):
        up_home, up_away = h - game["seen_home"], a - game["seen_away"]
        scorer, points = (home, up_home) if up_home >= up_away else (away, up_away)
        play = game["last_play"] or ""
        # One or two points on their own are the try after a touchdown already announced; a safety
        # (also two) is only called when the play says so.
        if points > 2 or (points == 2 and "safety" in play.lower()):
            label = scoring_label(points)
            body = line + (f" · {game['detail']}" if game["detail"] else "")
            # The latest play can lag the score by a poll; only quote it when it's clearly this score.
            if (label == "touchdown" and "touchdown" in play.lower()) or (label == "field goal" and "field goal" in play.lower()):
                body += f"\n{play}"
            events.append(("score", f"{a}-{h}", f"{scorer['short']} {label}", body))
    if game["state"] == "in" and None not in (h, a):
        events += model_alerts(game, home, away)
    return events


def ordinal_period(period):
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(period, "overtime")


def clock_seconds(clock):
    try:
        minutes, seconds = (clock or "").split(":")
        return int(minutes) * 60 + int(float(seconds))
    except ValueError:
        return None


def model_alerts(game, home, away):
    """Upset brewing and close-game alerts, each at most once per game (their push_events detail is fixed)."""
    events = []
    h, a, period = game["home_score"], game["away_score"], game["period"] or 0
    live, pre = game["home_win_prob"], game["pregame_prob"]
    live = None if live is None else (live / 100 if live > 1 else live)
    pre = None if pre is None else (pre / 100 if pre > 1 else pre)
    left = clock_seconds(game["clock"])
    where = f"in {ordinal_period(period)}" if period > 4 else f"in the {ordinal_period(period)}"

    # Upset brewing: a clear pregame favorite whose live chance has sunk, from the second half on.
    if pre is not None and live is not None and period >= 3:
        fav_home = pre >= 0.5
        fav, dog = (home, away) if fav_home else (away, home)
        fav_live = live if fav_home else 1 - live
        if max(pre, 1 - pre) >= UPSET_FAVORITE and 0.02 < fav_live <= UPSET_TROUBLE:
            margin = (h - a) if fav_home else (a - h)
            state = f"down {-margin}" if margin < 0 else "tied" if margin == 0 else f"up {margin}"
            timing = f"with {game['clock']} left {where}" if left else where
            events.append(("upset", "upset", f"Upset brewing: {dog['short']} vs. {fav['short']}",
                           f"{fav['short']} has a {round(fav_live * 100)}% chance, {state} {timing}."))

    # Close game: one score with two minutes or less in the 4th, or any overtime.
    if abs(h - a) <= CLOSE_MARGIN and (period > 4 or (period == 4 and left is not None and 0 < left <= CLOSE_SECONDS)):
        line = f"{away['abbr']} {a} – {home['abbr']} {h}"
        body = "Overtime." if period > 4 else ("Tied" if h == a else "One-score game") + f" with {game['clock']} left."
        events.append(("close", "close", f"Close game: {line}", body))
    return events


def record_state(conn, game):
    conn.execute(
        "INSERT INTO push_game_state (league, game_id, state, home_score, away_score) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (league, game_id) DO UPDATE SET state = EXCLUDED.state, home_score = EXCLUDED.home_score, "
        "away_score = EXCLUDED.away_score, updated_at = now()",
        (game["league"], game["game_id"], game["state"], game["home_score"], game["away_score"]))


def recipients(conn, league, team_ids, kind):
    column = PREFERENCE[kind]  # fixed names, never user input
    return conn.execute(
        f"""
        SELECT DISTINCT d.install_id, d.apns_token, d.environment, d.bundle_id
        FROM push_devices d JOIN push_follows f USING (install_id)
        WHERE f.league = %s AND f.team_id = ANY(%s) AND d.disabled_at IS NULL
          AND COALESCE(f.{column}, d.{column})  -- a per-team setting beats the device's
        """,
        (league, list(team_ids))).fetchall()


def deliver(conn, apns, devices, league, game_id, kind, title, body, data=None, thread_id=None):
    """Send one alert to each device; returns (sent, failed). Dead tokens are switched off."""
    sent = failed = 0
    for install_id, token, environment, bundle_id in devices:
        if apns is None:
            print(f"[notify] (no APNs key) would send {kind} to {install_id}: {title} | {body}", flush=True)
            continue
        result = apns.send(token, environment, bundle_id, {"title": title, "body": body},
                           data=data or {"league": league, "game_id": game_id, "kind": kind},
                           collapse_id=f"{league}-{game_id}-score" if kind == "score" else None,
                           thread_id=thread_id or f"{league}-{game_id}", expires_in=EXPIRES[kind])
        if result.ok:
            sent += 1
            continue
        failed += 1
        print(f"[notify] {kind} to {install_id} failed: {result.status} {result.reason}", flush=True)
        if result.dead_token:
            conn.execute("UPDATE push_devices SET disabled_at = now(), last_error = %s WHERE install_id = %s",
                         (result.reason or str(result.status), install_id))
    return sent, failed


def run_pass(conn, apns, dry_run=False):
    names = team_names(conn)
    now = datetime.now(timezone.utc)
    cur = conn.execute(GAMES)
    columns = [d.name for d in cur.description]
    alerts = 0
    for row in cur.fetchall():
        game = dict(zip(columns, row))
        first_sight = game["seen_state"] is None
        events = [] if first_sight else detect(game, names)
        stale = now - game["updated_at"] > STALE
        if dry_run:
            for kind, detail, title, body in events:
                devices = recipients(conn, game["league"], (game["home_team_id"], game["away_team_id"]), kind)
                print(f"[notify] dry run: {kind} {game['league']} {game['game_id']} -> {len(devices)} device(s): "
                      f"{title} | {body}{' (stale, not sent)' if stale else ''}", flush=True)
            continue
        for kind, detail, title, body in events:
            # Claim the alert first; if the row already exists, it was handled on an earlier pass.
            claimed = conn.execute(
                "INSERT INTO push_events (league, game_id, kind, detail, title, body) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (game["league"], game["game_id"], kind, detail, title, body)).fetchone()
            if not claimed or stale:
                continue
            devices = recipients(conn, game["league"], (game["home_team_id"], game["away_team_id"]), kind)
            sent, failed = deliver(conn, apns, devices, game["league"], game["game_id"], kind, title, body)
            conn.execute("UPDATE push_events SET sent = %s, failed = %s WHERE league = %s AND game_id = %s "
                         "AND kind = %s AND detail = %s", (sent, failed, game["league"], game["game_id"], kind, detail))
            alerts += 1
            print(f"[notify] {kind} {game['league']} {game['game_id']}: {title} -> {sent} sent, {failed} failed",
                  flush=True)
        if game["state"] == "in" and not stale:
            start_activities(conn, apns, game, names)
        record_state(conn, game)
    return alerts


AUTO_DEVICES = """
    SELECT DISTINCT d.install_id, d.activity_start_token, d.environment, d.bundle_id
    FROM push_devices d JOIN push_follows f USING (install_id)
    WHERE f.league = %s AND f.team_id = ANY(%s) AND d.disabled_at IS NULL AND d.auto_activities
      AND d.activity_start_token IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM push_activities a WHERE a.install_id = d.install_id AND a.league = f.league
                      AND a.game_id = %s AND a.ended_at IS NULL)
"""


def start_activities(conn, apns, game, names):
    """Auto-follow: start a Live Activity (push-to-start) on each opted-in device following either team, once per
    device and game. The device then registers the activity's update token, and activity_pass takes over."""
    devices = conn.execute(AUTO_DEVICES, (game["league"], [game["home_team_id"], game["away_team_id"]],
                                          game["game_id"])).fetchall()
    if not devices:
        return
    league = game["league"]

    def team(team_id):
        info = names.get((league, team_id), {})
        return {"id": team_id, "abbreviation": info.get("abbr", team_id), "name": info.get("short", team_id),
                "colorHex": info.get("color")}

    attributes = {"league": league, "gameId": game["game_id"], "away": team(game["away_team_id"]),
                  "home": team(game["home_team_id"])}
    state = activity_state(game)
    alert = {"title": f"{attributes['away']['name']} at {attributes['home']['name']}",
             "body": "Live now. Following on your Lock Screen."}
    for install_id, token, environment, bundle_id in devices:
        claimed = conn.execute(
            "INSERT INTO push_events (league, game_id, kind, detail, title, body) VALUES (%s, %s, 'activity_start', "
            "%s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
            (league, game["game_id"], install_id, alert["title"], alert["body"])).fetchone()
        if not claimed:
            continue
        if apns is None:
            print(f"[notify] (no APNs key) would start activity {league} {game['game_id']} on {install_id}", flush=True)
            continue
        result = apns.send_activity(token, environment, bundle_id, state, event="start",
                                    attributes_type="GameActivityAttributes", attributes=attributes, alert=alert)
        conn.execute("UPDATE push_events SET sent = %s, failed = %s WHERE league = %s AND game_id = %s "
                     "AND kind = 'activity_start' AND detail = %s",
                     (int(result.ok), int(not result.ok), league, game["game_id"], install_id))
        if not result.ok and result.dead_token:
            conn.execute("UPDATE push_devices SET activity_start_token = NULL WHERE install_id = %s", (install_id,))
        print(f"[notify] start activity {league} {game['game_id']} on {install_id}: "
              f"{'sent' if result.ok else f'failed {result.status} {result.reason}'}", flush=True)


NEWS = f"""
    SELECT a.id, a.league, a.kind, a.slug, a.headline, a.dek, a.game_id, a.published_at,
           array_agg(at.team_id) FILTER (WHERE {TEAM_STORY_SQL}) AS alert_teams,
           array_agg(at.team_id) AS all_teams
    FROM articles a JOIN article_teams at ON at.article_id = a.id
    WHERE a.status = 'published' AND a.published_at > now() - %s
      AND NOT EXISTS (SELECT 1 FROM push_events e WHERE e.kind = 'news' AND e.detail = 'article-' || a.id)
    GROUP BY a.id ORDER BY a.published_at
"""


def news_pass(conn, apns, dry_run=False):
    """Alert followers when a story about their team publishes: previews and recaps go to followers of the two
    teams in the game, power ratings and columns to followers of the teams they discuss. Teams only mentioned in
    passing (a previous opponent) don't alert. Tap opens the story (article_id / slug / url in the payload)."""
    names = team_names(conn)
    cur = conn.execute(NEWS, (NEWS_WINDOW,))
    columns = [d.name for d in cur.description]
    alerts = 0
    for row in cur.fetchall():
        a = dict(zip(columns, row))
        teams = [t for t in (a["alert_teams"] or []) if t]
        if not teams:
            continue
        label = NEWS_KIND.get(a["kind"], "Story")
        if a["kind"] in ("preview", "recap") and len(teams) == 2:
            abbrs = " vs. ".join(names.get((a["league"], t), {"abbr": t})["abbr"] for t in teams)
            title = f"{label}: {abbrs}"
        else:
            title = f"{a['league'].upper()} {label}"
        body = a["headline"]
        data = {"league": a["league"], "kind": "news", "article_kind": a["kind"], "article_id": str(a["id"]),
                "slug": a["slug"], "url": f"{SITE}/{a['league']}/news/{a['slug']}", "team_ids": a["all_teams"],
                "game_id": a["game_id"]}
        devices = recipients(conn, a["league"], teams, "news")
        if dry_run:
            print(f"[notify] dry run: news {a['league']} article {a['id']} -> {len(devices)} device(s): {title} | {body}",
                  flush=True)
            continue
        claimed = conn.execute(
            "INSERT INTO push_events (league, game_id, kind, detail, title, body) VALUES (%s, %s, 'news', %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            (a["league"], a["game_id"] or "-", f"article-{a['id']}", title, body)).fetchone()
        if not claimed:
            continue
        sent, failed = deliver(conn, apns, devices, a["league"], a["game_id"] or "-", "news", title, body,
                               data=data, thread_id=f"news-{a['league']}")
        conn.execute("UPDATE push_events SET sent = %s, failed = %s WHERE kind = 'news' AND detail = %s",
                     (sent, failed, f"article-{a['id']}"))
        alerts += 1
        print(f"[notify] news {a['league']} article {a['id']}: {title} | {body} -> {sent} sent, {failed} failed",
              flush=True)
    return alerts


def send_test(conn, apns, install_id):
    device = conn.execute("SELECT install_id, apns_token, environment, bundle_id FROM push_devices "
                          "WHERE install_id = %s", (install_id.lower(),)).fetchone()
    if not device:
        raise SystemExit(f"No registered device {install_id}.")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title, body = "SidelineWire test alert", "Push notifications are working."
    conn.execute("INSERT INTO push_events (league, game_id, kind, detail, title, body) VALUES "
                 "('-', '-', 'test', %s, %s, %s)", (f"{install_id} {stamp}", title, body))
    sent, failed = deliver(conn, apns, [device], "-", "-", "test", title, body)
    print(f"[notify] test to {install_id}: {sent} sent, {failed} failed", flush=True)


ACTIVITIES = """
    SELECT a.token, a.league, a.game_id, a.environment, a.bundle_id, a.last_state, a.created_at,
           l.state, l.detail, l.home_score, l.away_score, l.possession_team_id, l.down_distance, l.red_zone,
           l.home_win_prob, l.last_play
    FROM push_activities a LEFT JOIN live_games l USING (league, game_id)
    WHERE a.ended_at IS NULL
"""
ACTIVITY_MAX_AGE = timedelta(hours=12)   # an activity nobody ended (game never finished for us) is dropped after this
ACTIVITY_DISMISS = 2 * 3600              # a final stays on the lock screen this long


def activity_state(row):
    """The Live Activity's content state; keys match SidelineWire's GameActivityAttributes.ContentState."""
    state = {"pre": "pre", "in": "in", "post": "final"}.get(row["state"] or "pre", "pre")
    prob = row["home_win_prob"]
    return {"state": state, "detail": row["detail"] or "", "homeScore": row["home_score"] or 0,
            "awayScore": row["away_score"] or 0, "possessionTeamId": row["possession_team_id"] if state == "in" else None,
            "downDistance": row["down_distance"] if state == "in" else None, "redZone": bool(row["red_zone"]) and state == "in",
            "homeWinProb": None if prob is None else round(prob if prob <= 1 else prob / 100, 2),
            "lastPlay": (row["last_play"] or "")[:140] or None}


def activity_pass(conn, apns, dry_run=False):
    """Keep every registered Live Activity in step with its game: push the new content state whenever it changes
    (priority 10 for score or status changes, 5 for play-by-play), end it at the final, and drop dead tokens."""
    cur = conn.execute(ACTIVITIES)
    columns = [d.name for d in cur.description]
    now = datetime.now(timezone.utc)
    for row in (dict(zip(columns, r)) for r in cur.fetchall()):
        if row["state"] is None and now - row["created_at"] > ACTIVITY_MAX_AGE:
            if not dry_run:
                conn.execute("UPDATE push_activities SET ended_at = now(), last_error = 'expired' WHERE token = %s",
                             (row["token"],))
            continue
        if row["state"] is None:
            continue  # the game isn't in the live feed yet
        state = activity_state(row)
        last = row["last_state"] or {}
        if state == last:
            continue
        final = state["state"] == "final"
        big = (not last or state["state"] != last.get("state") or state["homeScore"] != last.get("homeScore")
               or state["awayScore"] != last.get("awayScore"))
        if dry_run:
            print(f"[notify] dry run: activity {row['league']} {row['game_id']} {row['token'][:8]}: "
                  f"{'end' if final else 'update'} {state}", flush=True)
            continue
        if apns is None:
            result_ok, reason = True, None
        else:
            result = apns.send_activity(row["token"], row["environment"], row["bundle_id"], state,
                                        event="end" if final else "update", priority=10 if big else 5,
                                        dismissal_date=now.timestamp() + ACTIVITY_DISMISS if final else None)
            result_ok, reason = result.ok, (None if result.ok else (result.reason or str(result.status)))
            if not result.ok and result.dead_token:
                conn.execute("UPDATE push_activities SET ended_at = now(), last_error = %s WHERE token = %s",
                             (reason, row["token"]))
                print(f"[notify] activity {row['token'][:8]} gone: {reason}", flush=True)
                continue
        if result_ok:
            conn.execute("UPDATE push_activities SET last_state = %s, updated_at = now(), last_error = NULL, "
                         "ended_at = CASE WHEN %s THEN now() END WHERE token = %s",
                         (Jsonb(state), final, row["token"]))
            print(f"[notify] activity {row['league']} {row['game_id']} {row['token'][:8]}: "
                  f"{'end' if final else 'update'} {state['awayScore']}-{state['homeScore']} {state['detail']}", flush=True)
        else:
            conn.execute("UPDATE push_activities SET last_error = %s WHERE token = %s", (reason, row["token"]))
            print(f"[notify] activity {row['token'][:8]} failed: {reason}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Push alerts for followed teams.")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print what one pass would send; write nothing")
    parser.add_argument("--test", metavar="INSTALL_ID", help="send a test alert to one registered device")
    args = parser.parse_args()

    apns = Apns.from_env()
    if apns is None:
        print("[notify] APNs key not configured; alerts will be logged, not sent", flush=True)
    with connect() as conn:
        init_db(conn)
        if args.test:
            return send_test(conn, apns, args.test)
        if args.dry_run:
            run_pass(conn, apns, dry_run=True)
            activity_pass(conn, apns, dry_run=True)
            return news_pass(conn, apns, dry_run=True)
        while True:
            try:
                run_pass(conn, apns)
                activity_pass(conn, apns)
                news_pass(conn, apns)
            except psycopg.OperationalError:
                raise  # connection lost: exit so Kubernetes restarts the pod with a fresh one
            except Exception as exc:  # anything else: keep the service up; the next pass retries
                print(f"[notify] pass failed: {type(exc).__name__}: {exc}", flush=True)
            if args.once:
                return
            time.sleep(EVERY)


if __name__ == "__main__":
    main()
