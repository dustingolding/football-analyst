import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

load_dotenv(Path(__file__).parent / ".env")

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_payloads (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT        NOT NULL DEFAULT 'espn',
    league      TEXT        NOT NULL,
    endpoint    TEXT        NOT NULL,
    params      JSONB       NOT NULL,
    status      INTEGER     NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB       NOT NULL,
    UNIQUE (source, league, endpoint, params)
);

CREATE INDEX IF NOT EXISTS raw_payloads_league_endpoint_idx
    ON raw_payloads (league, endpoint);

-- Everything below is rebuilt from raw_payloads by etl.py.

CREATE TABLE IF NOT EXISTS teams (
    league           TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    abbreviation     TEXT,
    location         TEXT,
    name             TEXT,
    display_name     TEXT,
    short_name       TEXT,
    color            TEXT,
    alternate_color  TEXT,
    logo             TEXT,
    conference_id    TEXT,        -- most recent conference seen; per-game conference is on games
    is_active        BOOLEAN,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, team_id)
);

CREATE TABLE IF NOT EXISTS games (
    league              TEXT        NOT NULL,
    game_id             TEXT        NOT NULL,
    season              INTEGER     NOT NULL,
    season_type         INTEGER     NOT NULL,   -- 2 = regular, 3 = postseason
    week                INTEGER,
    start_time          TIMESTAMPTZ,
    status              TEXT,                   -- STATUS_FINAL, STATUS_SCHEDULED, STATUS_CANCELED, ...
    completed           BOOLEAN     NOT NULL DEFAULT false,
    game_type           TEXT,                   -- STD, Bowl Game, Conference Championship, ...
    notes               TEXT,                   -- bowl / playoff round name
    neutral_site        BOOLEAN,
    conference_game     BOOLEAN,
    home_team_id        TEXT        NOT NULL,
    away_team_id        TEXT        NOT NULL,
    home_conference_id  TEXT,                   -- conference at the time of the game
    away_conference_id  TEXT,
    home_score          INTEGER,
    away_score          INTEGER,
    home_rank           INTEGER,                -- ESPN curatedRank for the game (poll rank); NULL = unranked
    away_rank           INTEGER,
    home_linescores     INTEGER[],
    away_linescores     INTEGER[],
    venue_id            TEXT,
    venue_name          TEXT,
    venue_city          TEXT,
    venue_state         TEXT,
    venue_indoor        BOOLEAN,
    attendance          INTEGER,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);

CREATE INDEX IF NOT EXISTS games_season_idx ON games (league, season, season_type, week);
CREATE INDEX IF NOT EXISTS games_home_team_idx ON games (league, home_team_id, start_time);
CREATE INDEX IF NOT EXISTS games_away_team_idx ON games (league, away_team_id, start_time);
"""


def database_url():
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def connect():
    return psycopg.connect(database_url(), autocommit=True)


def init_db(conn):
    conn.execute(SCHEMA)


def normalize_params(params):
    # Query params reach ESPN as strings, so store them that way; 2025 and "2025" are the same request.
    return {key: str(value) for key, value in (params or {}).items()}


def save_raw_payload(conn, league, endpoint, params, status, payload, source="espn"):
    conn.execute(
        """
        INSERT INTO raw_payloads (source, league, endpoint, params, status, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, league, endpoint, params) DO UPDATE
            SET status = EXCLUDED.status,
                payload = EXCLUDED.payload,
                fetched_at = now()
        """,
        (source, league, endpoint, Jsonb(normalize_params(params)), status, Jsonb(payload)),
    )


def get_raw_payload(conn, league, endpoint, params, source="espn"):
    row = conn.execute(
        """
        SELECT payload FROM raw_payloads
        WHERE source = %s AND league = %s AND endpoint = %s AND params = %s
        """,
        (source, league, endpoint, Jsonb(normalize_params(params))),
    ).fetchone()
    return row[0] if row else None
