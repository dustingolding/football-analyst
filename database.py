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

-- Per-player stats for a played game (e.g. QB dropback efficiency from nflverse play-by-play).
CREATE TABLE IF NOT EXISTS player_game_stats (
    league      TEXT        NOT NULL,
    game_id     TEXT        NOT NULL,   -- ESPN game_id
    player_id   TEXT        NOT NULL,   -- source's player id (nflverse: gsis id)
    team_id     TEXT        NOT NULL,   -- ESPN team_id
    source      TEXT        NOT NULL,
    player_name TEXT,
    stats       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id, player_id, source)
);

-- Per-team, per-season context (e.g. CFBD SP+, recruiting, talent, returning production).
CREATE TABLE IF NOT EXISTS team_seasons (
    league      TEXT        NOT NULL,
    season      INTEGER     NOT NULL,
    team_id     TEXT        NOT NULL,   -- ESPN team_id
    source      TEXT        NOT NULL,
    stats       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, season, team_id, source)
);

-- Conference / division membership per season (CFB realigns, so it's per season).
CREATE TABLE IF NOT EXISTS team_affiliations (
    league          TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    team_id         TEXT    NOT NULL,   -- ESPN team_id
    conference      TEXT,
    division        TEXT,
    classification  TEXT,               -- CFB: fbs, fcs, ii, iii
    PRIMARY KEY (league, season, team_id)
);

CREATE TABLE IF NOT EXISTS rosters (
    league      TEXT    NOT NULL,
    season      INTEGER NOT NULL,
    team_id     TEXT    NOT NULL,       -- ESPN team_id
    player_id   TEXT    NOT NULL,       -- NFL: gsis id; CFB: ESPN athlete id (same ids as box scores)
    source      TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    position    TEXT,
    jersey      INTEGER,
    height      INTEGER,                -- inches
    weight      INTEGER,
    experience  TEXT,                   -- NFL: years in the league; CFB: class year
    origin      TEXT,                   -- NFL: college; CFB: hometown
    headshot    TEXT,
    PRIMARY KEY (league, season, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS polls (
    league              TEXT    NOT NULL,
    season              INTEGER NOT NULL,
    season_type         INTEGER NOT NULL,   -- 2 = regular, 3 = postseason
    week                INTEGER NOT NULL,
    poll                TEXT    NOT NULL,   -- AP Top 25, Coaches Poll, Playoff Committee Rankings
    team_id             TEXT    NOT NULL,
    rank                INTEGER NOT NULL,
    points              INTEGER,
    first_place_votes   INTEGER,
    PRIMARY KEY (league, season, season_type, week, poll, team_id)
);

CREATE INDEX IF NOT EXISTS player_game_stats_team_idx ON player_game_stats (league, team_id, source);

-- Live game state from ESPN, written every ~30 s by live.py (the web app overlays it on games).
CREATE TABLE IF NOT EXISTS live_games (
    league              TEXT        NOT NULL,
    game_id             TEXT        NOT NULL,
    state               TEXT        NOT NULL,   -- pre, in, post
    detail              TEXT,                   -- "7:14 - 1st", "Halftime", "Final/OT"
    period              INTEGER,
    clock               TEXT,
    home_score          INTEGER,
    away_score          INTEGER,
    possession_team_id  TEXT,
    down_distance       TEXT,                   -- "3rd & 7 at CCU 45"
    red_zone            BOOLEAN,
    home_timeouts       INTEGER,
    away_timeouts       INTEGER,
    home_win_prob       DOUBLE PRECISION,       -- ESPN's in-game win probability
    last_play           TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);

ALTER TABLE live_games ADD COLUMN IF NOT EXISTS broadcast TEXT;
ALTER TABLE live_games ADD COLUMN IF NOT EXISTS home_record TEXT;
ALTER TABLE live_games ADD COLUMN IF NOT EXISTS away_record TEXT;

CREATE TABLE IF NOT EXISTS live_plays (
    league          TEXT    NOT NULL,
    game_id         TEXT    NOT NULL,
    play_id         TEXT    NOT NULL,
    sequence        BIGINT,
    drive           INTEGER,
    period          INTEGER,
    clock           TEXT,
    team_id         TEXT,
    play_type       TEXT,
    text            TEXT,
    home_score      INTEGER,
    away_score      INTEGER,
    scoring         BOOLEAN,
    home_win_prob   DOUBLE PRECISION,
    PRIMARY KEY (league, game_id, play_id)
);

-- Offseason player movement.
CREATE TABLE IF NOT EXISTS transfers (
    league          TEXT    NOT NULL,
    season          INTEGER NOT NULL,     -- the season the player transfers into
    transfer_key    TEXT    NOT NULL,     -- source-stable key (name + origin + date)
    name            TEXT    NOT NULL,
    position        TEXT,
    origin_team_id  TEXT,
    origin_name     TEXT,
    dest_team_id    TEXT,                 -- NULL: no destination (yet)
    dest_name       TEXT,
    stars           INTEGER,
    rating          DOUBLE PRECISION,     -- 247 transfer rating
    player_id       TEXT,                 -- matched ESPN athlete id (origin roster, prior season)
    PRIMARY KEY (league, transfer_key)
);

CREATE TABLE IF NOT EXISTS recruits (
    league      TEXT    NOT NULL,
    season      INTEGER NOT NULL,         -- signing class year (first season on campus)
    recruit_id  TEXT    NOT NULL,
    player_id   TEXT,                     -- ESPN athlete id when known
    name        TEXT    NOT NULL,
    position    TEXT,
    team_id     TEXT,
    stars       INTEGER,
    rating      DOUBLE PRECISION,
    ranking     INTEGER,
    PRIMARY KEY (league, recruit_id)
);

CREATE TABLE IF NOT EXISTS head_coaches (
    league      TEXT    NOT NULL,
    season      INTEGER NOT NULL,
    team_id     TEXT    NOT NULL,
    coach       TEXT    NOT NULL,         -- coached the most games that season
    games       INTEGER,
    hire_date   DATE,
    PRIMARY KEY (league, season, team_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    league      TEXT    NOT NULL,
    season      INTEGER NOT NULL,     -- draft year (the rookie's first season)
    pick        INTEGER NOT NULL,     -- overall pick
    round       INTEGER,
    team_id     TEXT,
    player_id   TEXT,                 -- gsis id
    name        TEXT,
    position    TEXT,
    PRIMARY KEY (league, season, pick)
);

-- News items (headline + publisher summary + link only; no article text).
CREATE TABLE IF NOT EXISTS news_items (
    league          TEXT        NOT NULL,
    article_id      TEXT        NOT NULL,
    published       TIMESTAMPTZ,
    headline        TEXT,
    description     TEXT,
    url             TEXT,
    team_ids        TEXT[],
    athletes        TEXT[],
    extracted_at    TIMESTAMPTZ,              -- when the LLM read it (NULL: not yet / not injury news)
    PRIMARY KEY (league, article_id)
);

-- Player availability reports: CFB from news (LLM-extracted), NFL from official injury reports.
CREATE TABLE IF NOT EXISTS player_status (
    league          TEXT        NOT NULL,
    source          TEXT        NOT NULL,     -- 'news_llm' or 'nfl_injury_report'
    source_id       TEXT        NOT NULL,     -- article id / game id
    team_id         TEXT        NOT NULL,
    player_name     TEXT        NOT NULL,
    player_id       TEXT,
    position        TEXT,
    status          TEXT        NOT NULL,     -- out, doubtful, questionable, probable, returning, suspended, season-ending
    games           INTEGER,                  -- games expected to miss, when stated
    published       TIMESTAMPTZ,
    headline        TEXT,
    url             TEXT,
    PRIMARY KEY (league, source, source_id, team_id, player_name)
);

-- Preseason team ratings from last season + offseason movement (offseason.py), fit only on
-- earlier seasons, with the rating broken down into contribution groups.
CREATE TABLE IF NOT EXISTS team_preseason (
    league          TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    team_id         TEXT    NOT NULL,
    rating          DOUBLE PRECISION NOT NULL,   -- projected SRS, points vs an average FBS team
    baseline        DOUBLE PRECISION,            -- last season's SRS
    actual          DOUBLE PRECISION,            -- this season's SRS so far (NULL before games)
    contributions   JSONB,                       -- points by group: history, recruiting, returning, ...
    features        JSONB,
    PRIMARY KEY (league, season, team_id)
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


def closing_lines(conn, league, game_ids=None):
    """One consensus line per game: the median across sportsbooks of the closing spread, total,
    opening spread, and de-vigged home win probability from the moneylines.

    Returns {game_id: {"spread", "total", "open_spread", "home_prob"}} (values may be None).
    game_ids limits it to those games (default: every game in the league).
    """
    home, away = IMPLIED_SQL.format(ml="home_moneyline"), IMPLIED_SQL.format(ml="away_moneyline")
    rows = conn.execute(
        f"""
        WITH books AS (
            SELECT game_id, home_spread, total, opening_home_spread,
                   CASE WHEN home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
                        THEN {home} / ({home} + {away}) END AS home_prob
            FROM odds
            WHERE league = %s AND home_spread IS NOT NULL AND (%s::text[] IS NULL OR game_id = ANY(%s::text[]))
        )
        SELECT game_id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY home_spread),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY total),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY opening_home_spread),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY home_prob)
        FROM books GROUP BY game_id
        """,
        (league, game_ids, game_ids),
    ).fetchall()
    return {
        game_id: {"spread": spread, "total": total, "open_spread": open_spread, "home_prob": prob}
        for game_id, spread, total, open_spread, prob in rows
    }


# American odds -> implied probability (vig included), as a SQL expression.
IMPLIED_SQL = "(CASE WHEN {ml} < 0 THEN -{ml}::float / (-{ml} + 100) ELSE 100.0 / ({ml} + 100) END)"
