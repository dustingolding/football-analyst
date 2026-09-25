"""College football injury news -> player availability, read by the local LLM (Ollama on the GPU).

1. Collects ESPN college football news: the league feed every run, and every FBS team's feed with
   --teams (daily). Only the headline, ESPN's summary, the link and tagged teams / athletes are kept.
2. Items that look like availability news (keyword filter) are read by the LLM, which returns the
   players whose status changed: name, team, position, status and games to miss.
3. Players are matched to the team's roster; rows go to player_status (source 'news_llm').

There is no injury-news history to train on, so these reports don't enter the game models as a
learned feature. features.py uses one of them through a relationship that is already learned: if a
team's presumed starting QB is reported out, the backup's QB rating is used instead.

    python cfb_news.py              # league feed + extraction
    python cfb_news.py --teams      # also every FBS team's feed (daily)
"""

import argparse
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests

from backfill import current_season
from database import connect, init_db

NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama.ai.svc.cluster.local:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
AVAILABILITY_WORDS = re.compile(
    r"injur|ruled out|out for|out (?:vs|against|this|saturday|friday|indefinitely)|questionable|doubtful|suspen|"
    r"sidelin|miss(?:es|ed|ing)? |surgery|torn|\bacl\b|week-to-week|day-to-day|concussion|ankle|knee|hamstring|"
    r"shoulder|return(?:s|ing)? (?:from|to)|expected to (?:play|start|return)|won't play|will not play|game-time",
    re.IGNORECASE)
STATUSES = {"out", "doubtful", "questionable", "probable", "returning", "suspended", "season-ending"}
PROMPT = """You extract college football player availability from a news item.
Return JSON: {{"players": [{{"name": str, "team": str, "position": str or null,
  "status": "out" | "doubtful" | "questionable" | "probable" | "returning" | "suspended" | "season-ending",
  "games": int or null}}]}}
Rules: include only players whose availability for upcoming games is stated or changes (injured, suspended,
returning, expected to play). "games" is how many games they will miss when stated (e.g. "miss Saturday's
game" = 1). Use "season-ending" only when the item says the player is out for the season or year;
"indefinitely", "extended time" or "foreseeable future" is "out". Return {{"players": []}} if none.

Teams tagged on the article: {teams}
Headline: {headline}
Summary: {description}"""


def norm(name):
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", re.sub(r"[^a-z ]", "", text.replace("-", " ")))
    return " ".join(text.split())


def fetch(conn, team_ids):
    """Store new articles from the league feed (and team feeds). Returns how many were new."""
    feeds = [{"limit": 100}] + [{"team": t, "limit": 50} for t in team_ids]
    new = 0
    for params in feeds:
        try:
            articles = requests.get(NEWS_URL, params=params, timeout=(5, 30)).json().get("articles", [])
        except (requests.RequestException, ValueError) as exc:
            print(f"[cfb_news] feed {params} failed: {exc}", flush=True)
            continue
        for a in articles:
            if not a.get("id"):
                continue
            cats = a.get("categories") or []
            teams = sorted({str(c.get("teamId") or (c.get("team") or {}).get("id")) for c in cats
                            if c.get("type") == "team" and (c.get("teamId") or (c.get("team") or {}).get("id"))})
            athletes = sorted({c.get("description") for c in cats if c.get("type") == "athlete" and c.get("description")})
            cur = conn.execute(
                "INSERT INTO news_items (league, article_id, published, headline, description, url, team_ids, athletes) "
                "VALUES ('cfb', %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (str(a["id"]), a.get("published"), a.get("headline"), a.get("description"),
                 ((a.get("links") or {}).get("web") or {}).get("href"), teams, athletes))
            new += cur.rowcount
        if len(feeds) > 1:
            time.sleep(0.2)
    return new


def ask(headline, description, team_names):
    response = requests.post(f"{OLLAMA_URL}/api/generate", timeout=(5, 180), json={
        "model": MODEL, "stream": False, "format": "json", "options": {"temperature": 0},
        "prompt": PROMPT.format(teams=", ".join(team_names) or "none", headline=headline or "",
                                description=description or ""),
    })
    response.raise_for_status()
    return json.loads(response.json()["response"]).get("players") or []


def team_lookup(conn):
    """Normalized team names (location, display, short, nickname) -> team_id, FBS/FCS alike."""
    names = {}
    for team_id, *labels in conn.execute(
        "SELECT team_id, display_name, location, short_name, name, abbreviation FROM teams WHERE league = 'cfb'"
    ):
        for label in labels:
            if label:
                names.setdefault(norm(label), team_id)
    return names


def extract(conn, limit):
    season = current_season()
    teams_by_id = dict(conn.execute("SELECT team_id, display_name FROM teams WHERE league = 'cfb'").fetchall())
    names = team_lookup(conn)
    roster = {}
    for team_id, player_id, name, position in conn.execute(
        "SELECT team_id, player_id, name, position FROM rosters WHERE league = 'cfb' AND season = %s", (season,)
    ):
        roster.setdefault(team_id, {})[norm(name)] = (player_id, position)

    items = conn.execute(
        "SELECT article_id, published, headline, description, url, team_ids FROM news_items "
        "WHERE league = 'cfb' AND extracted_at IS NULL ORDER BY published DESC LIMIT %s", (limit,)).fetchall()
    read = found = 0
    for article_id, published, headline, description, url, team_ids in items:
        text = f"{headline or ''} {description or ''}"
        if AVAILABILITY_WORDS.search(text):
            try:
                players = ask(headline, description, [teams_by_id.get(t, t) for t in team_ids or []])
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"[cfb_news] LLM failed on {article_id}: {exc}", flush=True)
                continue  # leave unextracted; retried next run
            read += 1
            for p in players:
                status = str(p.get("status") or "").lower()
                if status not in STATUSES or not p.get("name"):
                    continue
                team_id = names.get(norm(p.get("team")))
                if team_id is None and team_ids and len(team_ids) == 1:
                    team_id = team_ids[0]  # the article's only tagged team
                if team_id is None:
                    continue
                player_id, position = (roster.get(team_id) or {}).get(norm(p["name"]), (None, None))
                games = p.get("games") if isinstance(p.get("games"), int) and p.get("games") > 0 else None
                conn.execute(
                    "INSERT INTO player_status (league, source, source_id, team_id, player_name, player_id, position, "
                    "status, games, published, headline, url) VALUES ('cfb', 'news_llm', %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s) ON CONFLICT (league, source, source_id, team_id, player_name) DO UPDATE SET "
                    "status = EXCLUDED.status, games = EXCLUDED.games, player_id = EXCLUDED.player_id",
                    (article_id, team_id, p["name"], player_id, position or p.get("position"), status, games,
                     published, headline, url))
                found += 1
        conn.execute("UPDATE news_items SET extracted_at = now() WHERE league = 'cfb' AND article_id = %s",
                     (article_id,))
    print(f"[cfb_news] {len(items)} new items, {read} read by the LLM, {found} player statuses", flush=True)


def main():
    parser = argparse.ArgumentParser(description="CFB injury news -> player availability via the local LLM.")
    parser.add_argument("--teams", action="store_true", help="also fetch every FBS team's news feed (daily)")
    parser.add_argument("--limit", type=int, default=400, help="max new items to process per run")
    args = parser.parse_args()
    with connect() as conn:
        init_db(conn)
        team_ids = []
        if args.teams:
            team_ids = [t for (t,) in conn.execute(
                "SELECT team_id FROM team_affiliations WHERE league = 'cfb' AND season = %s AND classification = 'fbs'",
                (current_season(),))]
        new = fetch(conn, team_ids)
        print(f"[cfb_news] {new} new articles ({len(team_ids)} team feeds)", flush=True)
        extract(conn, args.limit)


if __name__ == "__main__":
    main()
