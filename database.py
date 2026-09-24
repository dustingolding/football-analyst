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

-- Betting lines per game and source, keyed by ESPN game_id. Spreads use the betting
-- convention from the home team's side: -3.5 means the home team is favored by 3.5.
CREATE TABLE IF NOT EXISTS odds (
    league               TEXT    NOT NULL,
    game_id              TEXT    NOT NULL,   -- ESPN game_id
    source               TEXT    NOT NULL,   -- nflverse, cfbd, espn
    provider             TEXT    NOT NULL,   -- sportsbook, or 'consensus'
    home_spread          NUMERIC,            -- closing
    total                NUMERIC,            -- closing
    home_moneyline       INTEGER,
    away_moneyline       INTEGER,
    home_spread_odds     INTEGER,
    away_spread_odds     INTEGER,
    over_odds            INTEGER,
    under_odds           INTEGER,
    opening_home_spread  NUMERIC,
    opening_total        NUMERIC,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id, source, provider)
);

-- Per-team box/efficiency stats for a played game, from sources other than ESPN (e.g. nflverse EPA).
CREATE TABLE IF NOT EXISTS team_game_stats (
    league      TEXT        NOT NULL,
    game_id     TEXT        NOT NULL,   -- ESPN game_id
    team_id     TEXT        NOT NULL,   -- ESPN team_id
    source      TEXT        NOT NULL,
    stats       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id, team_id, source)
);

-- One row per game of pre-game features (features.py); targets are NULL until the game is played.
CREATE TABLE IF NOT EXISTS game_features (
    league        TEXT        NOT NULL,
    game_id       TEXT        NOT NULL,
    season        INTEGER     NOT NULL,
    start_time    TIMESTAMPTZ NOT NULL,
    completed     BOOLEAN     NOT NULL,
    margin        DOUBLE PRECISION,       -- home minus away
    total_points  DOUBLE PRECISION,
    features      JSONB       NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);

-- Pre-game predictions written by batch jobs (elo.py, ...); the web app only reads these.
CREATE TABLE IF NOT EXISTS predictions (
    league            TEXT        NOT NULL,
    game_id           TEXT        NOT NULL,
    model             TEXT        NOT NULL,   -- elo, xgb, ...
    home_win_prob     DOUBLE PRECISION,
    predicted_margin  DOUBLE PRECISION,       -- home minus away
    predicted_total   DOUBLE PRECISION,
    details           JSONB,                  -- model-specific (ratings, params)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id, model)
);
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


def closing_lines(conn, league):
    """One consensus line per game: the median across sportsbooks of the closing spread, total,
    opening spread, and de-vigged home win probability from the moneylines.

    Returns {game_id: {"spread", "total", "open_spread", "home_prob"}} (values may be None).
    """
    home, away = IMPLIED_SQL.format(ml="home_moneyline"), IMPLIED_SQL.format(ml="away_moneyline")
    rows = conn.execute(
        f"""
        WITH books AS (
            SELECT game_id, home_spread, total, opening_home_spread,
                   CASE WHEN home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
                        THEN {home} / ({home} + {away}) END AS home_prob
            FROM odds
            WHERE league = %s AND home_spread IS NOT NULL
        )
        SELECT game_id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY home_spread),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY total),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY opening_home_spread),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY home_prob)
        FROM books GROUP BY game_id
        """,
        (league,),
    ).fetchall()
    return {
        game_id: {"spread": spread, "total": total, "open_spread": open_spread, "home_prob": prob}
        for game_id, spread, total, open_spread, prob in rows
    }


# American odds -> implied probability (vig included), as a SQL expression.
IMPLIED_SQL = "(CASE WHEN {ml} < 0 THEN -{ml}::float / (-{ml} + 100) ELSE 100.0 / ({ml} + 100) END)"
