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


def number(value, cast=float):
    return cast(float(value)) if value not in (None, "", "NA") else None


def parse_odds(row):
    if not row.get("espn") or number(row.get("spread_line")) is None:
        return None
    return {
        "league": "nfl",
        "game_id": row["espn"],
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
    row = conn.execute(
        "SELECT payload FROM raw_payloads WHERE source = %s AND league = 'nfl' AND endpoint = %s",
        (SOURCE, ENDPOINT),
    ).fetchone()
    if row is None:
        raise SystemExit("No stored nflverse games.csv; run without --no-fetch first.")

    known = {game_id for (game_id,) in conn.execute("SELECT game_id FROM games WHERE league = 'nfl'")}
    odds = [o for o in map(parse_odds, row[0]) if o]
    matched = [o for o in odds if o["game_id"] in known]

    placeholders = ", ".join(["%s"] * len(ODDS_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ODDS_COLUMNS[4:])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM odds WHERE league = 'nfl' AND source = %s", (SOURCE,))
        cur.executemany(
            f"INSERT INTO odds ({', '.join(ODDS_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT (league, game_id, source, provider) DO UPDATE SET {updates}, updated_at = now()",
            [[o[c] for c in ODDS_COLUMNS] for o in matched],
        )
    print(f"[nflverse] {len(matched)} games with odds loaded "
          f"({len(odds) - len(matched)} not in games table, e.g. before the ESPN backfill range)", flush=True)


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
