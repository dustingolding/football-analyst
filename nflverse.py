"""Load NFL closing lines from nflverse into the odds table.

nflverse's games.csv has closing spread, total, moneylines and juice for every game
since 1999, plus the ESPN game id, so rows join straight onto games.game_id.

    python nflverse.py              # fetch games.csv, store it raw, rebuild nflverse odds
    python nflverse.py --no-fetch   # rebuild odds from the stored copy only
"""

import argparse
import csv
import io

import requests

from database import connect, init_db, save_raw_payload

GAMES_CSV_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
SOURCE = "nflverse"
ENDPOINT = "games.csv"

ODDS_COLUMNS = [
    "league", "game_id", "source", "provider", "home_spread", "total",
    "home_moneyline", "away_moneyline", "home_spread_odds", "away_spread_odds", "over_odds", "under_odds",
]


def fetch_games(conn):
    response = requests.get(GAMES_CSV_URL, timeout=(10, 120))
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    save_raw_payload(conn, "nfl", ENDPOINT, {}, response.status_code, rows, source=SOURCE)
    print(f"[nflverse] fetched {len(rows)} games", flush=True)


def stored_rows(conn):
    row = conn.execute(
        "SELECT payload FROM raw_payloads WHERE source = %s AND league = 'nfl' AND endpoint = %s",
        (SOURCE, ENDPOINT),
    ).fetchone()
    if row is None:
        raise SystemExit("No stored nflverse games.csv; run nflverse.py without --no-fetch first.")
    return row[0]


def matched_rows(conn, rows):
    """Yield (espn_game_id, row) pairs, one per ESPN game.

    nflverse's espn column is wrong for a few older games (two games share an id), so an
    id is only trusted when the final score agrees or the game hasn't been played. Rows
    that fail that check are matched on Eastern date + final score instead.
    """
    games = {}
    by_date_score = {}
    for game_id, day, home, away in conn.execute(
        "SELECT game_id, (start_time AT TIME ZONE 'America/New_York')::date::text, home_score, away_score "
        "FROM games WHERE league = 'nfl'"
    ):
        games[game_id] = (home, away)
        if home is not None:
            by_date_score[(day, home, away)] = game_id

    claimed = {}
    for row in rows:
        score = (number(row.get("home_score"), int), number(row.get("away_score"), int))
        game_id = row.get("espn")
        if game_id in games and (score[0] is None or games[game_id][0] is None or games[game_id] == score):
            verified = score[0] is not None and games[game_id] == score
        else:
            game_id, verified = by_date_score.get((row.get("gameday"), *score)), True
        if game_id and (game_id not in claimed or (verified and not claimed[game_id][0])):
            claimed[game_id] = (verified, row)
    for game_id, (_, row) in claimed.items():
        yield game_id, row


def number(value, cast=float):
    return cast(float(value)) if value not in (None, "", "NA") else None


def parse_odds(game_id, row):
    if number(row.get("spread_line")) is None:
        return None
    return {
        "league": "nfl",
        "game_id": game_id,
        "source": SOURCE,
        "provider": "consensus",
        # nflverse spread_line is the home team's expected margin (positive = home favored).
        "home_spread": -number(row["spread_line"]),
        "total": number(row.get("total_line")),
        "home_moneyline": number(row.get("home_moneyline"), int),
        "away_moneyline": number(row.get("away_moneyline"), int),
        "home_spread_odds": number(row.get("home_spread_odds"), int),
        "away_spread_odds": number(row.get("away_spread_odds"), int),
        "over_odds": number(row.get("over_odds"), int),
        "under_odds": number(row.get("under_odds"), int),
    }


def load_odds(conn):
    rows = stored_rows(conn)
    matched = [o for o in (parse_odds(game_id, row) for game_id, row in matched_rows(conn, rows)) if o]

    placeholders = ", ".join(["%s"] * len(ODDS_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ODDS_COLUMNS[4:])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM odds WHERE league = 'nfl' AND source = %s", (SOURCE,))
        cur.executemany(
            f"INSERT INTO odds ({', '.join(ODDS_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT (league, game_id, source, provider) DO UPDATE SET {updates}, updated_at = now()",
            [[o[c] for c in ODDS_COLUMNS] for o in matched],
        )
    print(f"[nflverse] {len(matched)} games with odds loaded ({len(rows)} nflverse games, from 1999)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Load nflverse NFL closing lines into odds.")
    parser.add_argument("--no-fetch", action="store_true", help="use the stored games.csv instead of downloading")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if not args.no_fetch:
            fetch_games(conn)
        load_odds(conn)


if __name__ == "__main__":
    main()
