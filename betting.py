"""Mock betting ("Beat the Model"): prices, the model's side, grading and standings.

Players are anonymous ids from the app (no accounts) with a nickname for leaderboards. Each gets a
1,000-unit bankroll; bets are spread, moneyline or total, one per market per game, placed before
kickoff at the consensus line locked when placed. Every bet records the model's side of the same
market, so after grading a bet that tailed the model shares its result and one that faded it gets
the opposite (pushes stay pushes). notify.py grades open bets once games are final.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BANKROLL = 1000
MIN_STAKE, MAX_STAKE = 1, 500
DEFAULT_PRICE = -110          # spreads and totals when books don't list a price
RESET_BELOW = 10              # a bankroll can be reset once it's under this, with nothing open
MARKETS = {"spread": ("home", "away"), "moneyline": ("home", "away"), "total": ("over", "under")}
# The model whose margin and total drive the spread and total picks (keep in step with app.INDEPENDENT_MODEL).
INDEPENDENT = {"nfl": "linear", "cfb": "xgb"}
EASTERN = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bet_players (
    player_id   TEXT        PRIMARY KEY,          -- random per player, kept by the app
    nickname    TEXT        NOT NULL,
    reset_at    TIMESTAMPTZ,                      -- bankroll counts bets placed after this
    resets      INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS bet_players_nickname_idx ON bet_players (lower(nickname));

CREATE TABLE IF NOT EXISTS bets (
    id               BIGSERIAL   PRIMARY KEY,
    player_id        TEXT        NOT NULL REFERENCES bet_players (player_id) ON DELETE CASCADE,
    league           TEXT        NOT NULL,
    game_id          TEXT        NOT NULL,
    season           INTEGER     NOT NULL,
    market           TEXT        NOT NULL,     -- spread, moneyline, total
    selection        TEXT        NOT NULL,     -- home/away, or over/under
    line             NUMERIC,                  -- the selection's spread, or the total; NULL for moneyline
    price            INTEGER     NOT NULL,     -- American odds
    stake            NUMERIC     NOT NULL,
    model_selection  TEXT,                     -- the model's side of this market when the bet was placed
    status           TEXT        NOT NULL DEFAULT 'open',   -- open, won, lost, push
    profit           NUMERIC,                  -- units won or lost, once graded
    placed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at       TIMESTAMPTZ,
    UNIQUE (player_id, league, game_id, market)
);
CREATE INDEX IF NOT EXISTS bets_open_idx ON bets (league, game_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS bets_player_idx ON bets (player_id, placed_at DESC);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_ro') THEN
        GRANT SELECT, INSERT, UPDATE ON bet_players TO web_ro;
        GRANT SELECT, INSERT, DELETE ON bets TO web_ro;
        GRANT USAGE ON SEQUENCE bets_id_seq TO web_ro;
    END IF;
END $$;
"""


# --- prices --------------------------------------------------------------------------------------

def half(value):
    """Lines move in half points."""
    return None if value is None else round(float(value) * 2) / 2


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def price_or_default(values):
    m = median(values)
    return DEFAULT_PRICE if m is None else int(round(m))


def game_state(conn, league, game_id):
    """(game row, locked) where locked means kickoff has passed or the game is under way."""
    row = conn.execute(
        """
        SELECT g.season, g.start_time, g.completed, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
               l.state AS live_state
        FROM games g LEFT JOIN live_games l USING (league, game_id)
        WHERE g.league = %s AND g.game_id = %s
        """, (league, game_id)).fetchone()
    if not row:
        return None, True
    keys = ("season", "start_time", "completed", "home_team_id", "away_team_id", "home_score", "away_score",
            "live_state")
    game = dict(zip(keys, row))
    started = game["start_time"] is None or game["start_time"] <= datetime.now(timezone.utc)
    locked = game["completed"] or started or (game["live_state"] or "pre") != "pre"
    return game, locked


def model_sides(conn, league, game_id, spread, total):
    """The model's side of each market: spread from the independent model's margin against the line,
    moneyline from the best available win probability, total from the projected total against the line."""
    preds = {m: (p, margin, t) for m, p, margin, t in conn.execute(
        "SELECT model, home_win_prob, predicted_margin, predicted_total FROM predictions "
        "WHERE league = %s AND game_id = %s", (league, game_id)).fetchall()}
    sides = {"spread": None, "moneyline": None, "total": None}
    ind = preds.get(INDEPENDENT[league])
    if ind and ind[1] is not None and spread is not None:
        edge = float(ind[1]) + float(spread)      # > 0: the model has the home side covering
        sides["spread"] = "home" if edge > 0 else "away" if edge < 0 else None
    prob = next((preds[m][0] for m in ("xgb_market", INDEPENDENT[league], "elo")
                 if m in preds and preds[m][0] is not None), None)
    if prob is not None:
        sides["moneyline"] = "home" if float(prob) >= 0.5 else "away"
    proj = next((preds[m][2] for m in ("xgb_market", INDEPENDENT[league]) if m in preds and preds[m][2] is not None),
                None)
    if proj is not None and total is not None and float(proj) != float(total):
        sides["total"] = "over" if float(proj) > float(total) else "under"
    return sides


def markets(conn, league, game_id):
    """Current consensus prices for a game (median across books) and the model's side of each; None if the
    game doesn't exist. Spread lines are per side (the away line is minus the home line)."""
    game, locked = game_state(conn, league, game_id)
    if game is None:
        return None
    books = conn.execute(
        "SELECT home_spread, total, home_moneyline, away_moneyline, home_spread_odds, away_spread_odds, "
        "over_odds, under_odds FROM odds WHERE league = %s AND game_id = %s", (league, game_id)).fetchall()
    col = lambda i: [float(b[i]) if b[i] is not None else None for b in books]  # noqa: E731
    spread, total = half(median(col(0))), half(median(col(1)))
    home_ml, away_ml = median(col(2)), median(col(3))
    out = {"locked": locked, "spread": None, "total": None, "moneyline": None,
           "model": model_sides(conn, league, game_id, spread, total)}
    if spread is not None:
        out["spread"] = {"home_line": spread, "away_line": -spread,
                         "home_price": price_or_default(col(4)), "away_price": price_or_default(col(5))}
    if total is not None:
        out["total"] = {"line": total, "over_price": price_or_default(col(6)), "under_price": price_or_default(col(7))}
    if home_ml is not None and away_ml is not None:
        out["moneyline"] = {"home_price": int(round(home_ml)), "away_price": int(round(away_ml))}
    return out


def quote(market_prices, market, selection):
    """(line, price) for one selection, or None if that market has no price."""
    m = market_prices.get(market)
    if not m:
        return None
    if market == "spread":
        return (m[f"{selection}_line"], m[f"{selection}_price"])
    if market == "total":
        return (m["line"], m[f"{selection}_price"])
    return (None, m[f"{selection}_price"])


# --- grading -------------------------------------------------------------------------------------

def win_amount(stake, price):
    """Profit on a winning bet at American odds."""
    stake = float(stake)
    return round(stake * price / 100 if price > 0 else stake * 100 / -price, 2)


def outcome(market, selection, line, home, away):
    """won / lost / push for a bet given the final score."""
    if market == "total":
        points = home + away
        diff = points - float(line) if selection == "over" else float(line) - points
    else:
        margin = (home - away) if selection == "home" else (away - home)
        diff = margin + (float(line) if market == "spread" else 0)
    return "won" if diff > 0 else "lost" if diff < 0 else "push"


def model_result(status, selection, model_selection):
    """How the model did on the same bet: tailing shares the result, fading gets the opposite."""
    if model_selection is None or status == "push":
        return None if model_selection is None else "push"
    if model_selection == selection:
        return status
    return {"won": "lost", "lost": "won"}.get(status)


def settle(conn):
    """Grade every open bet whose game is final. Returns how many were graded."""
    rows = conn.execute(
        """
        SELECT b.id, b.market, b.selection, b.line, b.price, b.stake, g.home_score, g.away_score
        FROM bets b JOIN games g USING (league, game_id)
        LEFT JOIN live_games l USING (league, game_id)
        WHERE b.status = 'open' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND (g.completed OR l.state = 'post')
        """).fetchall()
    for bet_id, market, selection, line, price, stake, home, away in rows:
        status = outcome(market, selection, line, home, away)
        profit = win_amount(stake, price) if status == "won" else -float(stake) if status == "lost" else 0
        conn.execute("UPDATE bets SET status = %s, profit = %s, settled_at = now() WHERE id = %s AND status = 'open'",
                     (status, profit, bet_id))
    return len(rows)


# --- standings -----------------------------------------------------------------------------------

def week_start(now=None):
    """Football weeks run Tuesday 4 a.m. Eastern to the next Tuesday (after Monday night)."""
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    start = (now - timedelta(days=(now.weekday() - 1) % 7)).replace(hour=4, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=7)
    return start.astimezone(timezone.utc)


def summarize(bets):
    """Record, profit and ROI for a list of (status, stake, profit, selection, model_selection)."""
    s = {"won": 0, "lost": 0, "push": 0, "open": 0, "profit": 0.0, "staked": 0.0,
         "tail": {"won": 0, "lost": 0, "push": 0}, "fade": {"won": 0, "lost": 0, "push": 0}}
    for status, stake, profit, selection, model_selection in bets:
        s[status] += 1
        if status == "open":
            continue
        s["profit"] += float(profit or 0)
        s["staked"] += float(stake)
        if model_selection:
            s["tail" if model_selection == selection else "fade"][status] += 1
    s["profit"] = round(s["profit"], 2)
    s["roi"] = round(s["profit"] / s["staked"], 4) if s["staked"] else None
    return s


def bankroll(conn, player_id, reset_at):
    """(balance, available): units after graded bets since the last reset, and that minus open stakes."""
    graded, open_stakes = conn.execute(
        "SELECT COALESCE(sum(profit) FILTER (WHERE status <> 'open'), 0), "
        "COALESCE(sum(stake) FILTER (WHERE status = 'open'), 0) FROM bets "
        "WHERE player_id = %s AND (%s::timestamptz IS NULL OR placed_at > %s::timestamptz)",
        (player_id, reset_at, reset_at)).fetchone()
    balance = round(BANKROLL + float(graded), 2)
    return balance, round(balance - float(open_stakes), 2)


def model_record(conn, season, since=None):
    """The model's own flat-100 record against the spread on every graded game this season (or since a time):
    it 'bets' its spread side on every game with a line, at -110."""
    from database import closing_lines  # noqa: PLC0415  (database imports nothing from here)
    won = lost = push = 0
    for league in INDEPENDENT:
        games = conn.execute(
            "SELECT g.game_id, g.home_score, g.away_score, p.predicted_margin FROM games g "
            "JOIN predictions p ON p.league = g.league AND p.game_id = g.game_id AND p.model = %s "
            "WHERE g.league = %s AND g.season = %s AND g.completed AND g.home_score IS NOT NULL "
            "AND p.predicted_margin IS NOT NULL AND (%s::timestamptz IS NULL OR g.start_time >= %s::timestamptz)",
            (INDEPENDENT[league], league, season, since, since)).fetchall()
        lines = closing_lines(conn, league, [g[0] for g in games]) if games else {}
        for game_id, home, away, margin in games:
            spread = (lines.get(game_id) or {}).get("spread")
            if spread is None or float(margin) + float(spread) == 0:
                continue
            side = "home" if float(margin) + float(spread) > 0 else "away"
            line = float(spread) if side == "home" else -float(spread)
            result = outcome("spread", side, line, home, away)
            won, lost, push = won + (result == "won"), lost + (result == "lost"), push + (result == "push")
    profit = round(won * win_amount(100, DEFAULT_PRICE) - lost * 100, 2)
    staked = (won + lost) * 100
    return {"won": won, "lost": lost, "push": push, "profit": profit,
            "roi": round(profit / staked, 4) if staked else None}
