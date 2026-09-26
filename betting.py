"""Mock betting ("Beat the Model"): prices, the model's side, grading and standings.

Players are random ids from the app (linked to an account once signed in) with a nickname for leaderboards. Each gets a
1,000-unit bankroll; bets are spread, moneyline or total, one per market per game, placed before
kickoff at the consensus line locked when placed. Every bet records the model's side of the same
market, so after grading a bet that tailed the model shares its result and one that faded it gets
the opposite (pushes stay pushes). notify.py grades open bets once games are final.

Parlays are one stake on 2-6 legs, each leg's price locked when placed; the payout multiplies the legs'
decimal odds (capped at +10000). Legs can share a game (player props, a side and a total) but not repeat a
market, and a game's spread and moneyline can't both be in one. A losing leg loses the parlay as soon as it's
graded; a pushed or void leg drops out (its odds count as 1); all legs pushing refunds the stake.

In-game bets: once a game is under way the spread, total and moneyline come from SidelineWire's live line
(live_markets: the live win probability and the clock), not the books. They're singles, close with two
minutes left, and wait LIVE_DELAY seconds: if the score changes before the feed confirms the bet, it's void
(refunded), so a touchdown seen on TV before the feed updates can't be bet against a stale line.

Player props (props): over/under on a team's main passer, rushers and receivers, lined from their recent
final box scores (game_boxscores) at PROP_PRICE, pregame only, graded from the final box score; a player who
doesn't appear is void.
"""

import math
import re
from statistics import NormalDist, mean

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BANKROLL = 1000
MIN_STAKE, MAX_STAKE = 1, 500
DEFAULT_PRICE = -110          # spreads and totals when books don't list a price
RESET_BELOW = 10              # a bankroll can be reset once it's under this, with nothing open
MARKETS = {"spread": ("home", "away"), "moneyline": ("home", "away"), "total": ("over", "under"),
           "prop": ("over", "under")}
# The model whose margin and total drive the spread and total picks (keep in step with app.INDEPENDENT_MODEL).
INDEPENDENT = {"nfl": "linear", "cfb": "xgb"}
EASTERN = ZoneInfo("America/New_York")
MIN_LEGS, MAX_LEGS = 2, 6
MAX_PARLAY_ODDS = 101.0       # decimal, i.e. +10000
LIVE_DELAY = 30               # seconds an in-game bet waits for the feed to confirm the score hasn't changed
LIVE_STALE = 120              # no in-game prices from a live row older than this
LIVE_CUTOFF = 120             # in-game betting closes with this many seconds left (and in overtime)
LIVE_SIGMA = {"nfl": 13.5, "cfb": 16.0}   # spread of final margins over a whole game, in points
LIVE_HOLD = 0.045             # the live moneyline's built-in margin
PROP_PRICE = -115
# stat -> (box score category, column label, name); players are picked by the first column.
PROP_STATS = {"pass_yds": ("passing", "YDS", "Passing Yards", "Pass Yds"),
              "rush_yds": ("rushing", "YDS", "Rushing Yards", "Rush Yds"),
              "rec_yds": ("receiving", "YDS", "Receiving Yards", "Rec Yds")}
PROP_PLAYERS = {"pass_yds": 1, "rush_yds": 2, "rec_yds": 3}   # per team

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
-- False from grading until notify.py has sent the result alert (bets graded before this column: never).
ALTER TABLE bets ADD COLUMN IF NOT EXISTS notified BOOLEAN NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS parlays (
    id          BIGSERIAL   PRIMARY KEY,
    player_id   TEXT        NOT NULL REFERENCES bet_players (player_id) ON DELETE CASCADE,
    season      INTEGER     NOT NULL,
    stake       NUMERIC     NOT NULL,
    odds        NUMERIC     NOT NULL,             -- decimal odds of all legs, as placed
    status      TEXT        NOT NULL DEFAULT 'open',   -- open, won, lost, push
    profit      NUMERIC,
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at  TIMESTAMPTZ,
    notified    BOOLEAN     NOT NULL DEFAULT true -- false from settling until the result alert is sent
);
CREATE INDEX IF NOT EXISTS parlays_player_idx ON parlays (player_id, placed_at DESC);

CREATE TABLE IF NOT EXISTS parlay_legs (
    id               BIGSERIAL PRIMARY KEY,
    parlay_id        BIGINT    NOT NULL REFERENCES parlays (id) ON DELETE CASCADE,
    league           TEXT      NOT NULL,
    game_id          TEXT      NOT NULL,
    market           TEXT      NOT NULL,
    selection        TEXT      NOT NULL,
    line             NUMERIC,
    price            INTEGER   NOT NULL,
    model_selection  TEXT,
    status           TEXT      NOT NULL DEFAULT 'open',
    UNIQUE (parlay_id, league, game_id)            -- (replaced below: legs may share a game)
);
CREATE INDEX IF NOT EXISTS parlay_legs_open_idx ON parlay_legs (league, game_id) WHERE status = 'open';

-- In-game bets and player props.
ALTER TABLE bets ADD COLUMN IF NOT EXISTS live BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE bets ADD COLUMN IF NOT EXISTS pending_until TIMESTAMPTZ;   -- in-game: confirmed after this if the score held
ALTER TABLE bets ADD COLUMN IF NOT EXISTS placed_home INTEGER;         -- the score when an in-game bet was placed
ALTER TABLE bets ADD COLUMN IF NOT EXISTS placed_away INTEGER;
ALTER TABLE bets ADD COLUMN IF NOT EXISTS placed_detail TEXT;          -- the clock then, e.g. "5:12 - 3rd"
ALTER TABLE bets ADD COLUMN IF NOT EXISTS prop TEXT;                   -- "<athlete id>:<stat>" for market 'prop'
ALTER TABLE bets ADD COLUMN IF NOT EXISTS prop_name TEXT;              -- "Josh Allen · Pass Yds"
ALTER TABLE bets DROP CONSTRAINT IF EXISTS bets_player_id_league_game_id_market_key;
CREATE UNIQUE INDEX IF NOT EXISTS bets_one_pregame_idx ON bets (player_id, league, game_id, market, COALESCE(prop, ''))
    WHERE NOT live;                                                    -- in-game bets can repeat a market
ALTER TABLE parlay_legs ADD COLUMN IF NOT EXISTS prop TEXT;
ALTER TABLE parlay_legs ADD COLUMN IF NOT EXISTS prop_name TEXT;
ALTER TABLE parlay_legs DROP CONSTRAINT IF EXISTS parlay_legs_parlay_id_league_game_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS parlay_legs_one_idx ON parlay_legs (parlay_id, league, game_id, market, COALESCE(prop, ''));

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_ro') THEN
        GRANT SELECT, INSERT, DELETE ON parlays, parlay_legs TO web_ro;
        GRANT USAGE ON SEQUENCE parlays_id_seq, parlay_legs_id_seq TO web_ro;
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
    if locked and not game["completed"]:
        live, reason = live_markets(conn, league, game_id, total)
        if live is None:
            return {"locked": True, "live": game["live_state"] == "in", "reason": reason, "spread": None,
                    "total": None, "moneyline": None, "model": {"spread": None, "moneyline": None, "total": None}}
        return {"locked": False, "reason": None, "model": {"spread": None, "moneyline": None, "total": None}, **live}
    out = {"locked": locked, "live": False, "reason": "Betting on this game is closed." if locked else None,
           "spread": None, "total": None, "moneyline": None,
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

# --- in-game prices ------------------------------------------------------------------------------

def seconds_left(period, clock):
    """Regulation seconds left from the period and "MM:SS" clock; 0 in overtime, None if unknown."""
    if period is None:
        return None
    if period > 4:
        return 0
    m = re.match(r"^(\d+):(\d+)", clock or "")
    secs = int(m.group(1)) * 60 + int(m.group(2)) if m else 0
    return (4 - period) * 900 + secs


def price_for_prob(prob):
    """American odds for a win probability with the live margin built in."""
    q = min(max(prob + LIVE_HOLD / 2, 0.01), 0.99)
    return -round(100 * q / (1 - q)) if q >= 0.5 else round(100 * (1 - q) / q)


def live_markets(conn, league, game_id, pregame_total):
    """SidelineWire's live line for a game under way, or (None, reason) when in-game betting is closed.
    The spread is the final margin the live win probability implies with the time left; the total is the
    points so far plus the pregame total's pace for the time left; the moneyline is the probability itself."""
    row = conn.execute(
        "SELECT state, period, clock, home_score, away_score, home_win_prob, detail, updated_at > now() - %s::interval "
        "FROM live_games WHERE league = %s AND game_id = %s", (f"{LIVE_STALE} seconds", league, game_id)).fetchone()
    if not row or row[0] != "in":
        return None, "Betting on this game is closed."
    state, period, clock, home, away, prob, detail, fresh = row
    left = seconds_left(period, clock)
    if not fresh or prob is None or home is None or away is None or left is None:
        return None, "In-game betting is paused while the live feed catches up."
    if left < LIVE_CUTOFF:
        return None, "In-game betting closes with two minutes left."
    p = min(max(float(prob) / 100 if float(prob) > 1 else float(prob), 0.005), 0.995)
    frac = left / 3600
    sigma = LIVE_SIGMA.get(league, 14.0) * math.sqrt(frac)
    margin = sigma * NormalDist().inv_cdf(p)          # expected final home margin
    spread = half(-margin)
    out = {"live": True, "detail": detail, "home_score": home, "away_score": away,
           "spread": {"home_line": spread, "away_line": -spread, "home_price": DEFAULT_PRICE,
                      "away_price": DEFAULT_PRICE},
           "total": None, "moneyline": None}
    if pregame_total is not None:
        out["total"] = {"line": half(home + away + float(pregame_total) * frac) or 0.5,
                        "over_price": DEFAULT_PRICE, "under_price": DEFAULT_PRICE}
    if 0.04 <= p <= 0.96:   # no moneyline on a game that's all but decided
        out["moneyline"] = {"home_price": price_for_prob(p), "away_price": price_for_prob(1 - p)}
    return out, None


# --- player props ---------------------------------------------------------------------------------

def box_stats(data):
    """{(team_id, stat): {athlete: (name, value, volume)}} from a game_boxscores payload."""
    out = {}
    for team in data or []:
        for cat in team.get("categories") or []:
            for stat, (category, label, _, _) in PROP_STATS.items():
                if cat.get("name") != category:
                    continue
                labels = cat.get("labels") or []
                if label not in labels:
                    continue
                i = labels.index(label)
                for player in cat.get("players") or []:
                    if not str(player.get("id", "")).isdigit():
                        continue   # the "Team" row
                    stats = player.get("stats") or []
                    try:
                        value = float(str(stats[i]).replace(",", ""))
                        first = str(stats[0])
                        volume = float(first.split("/")[-1]) if "/" in first else float(first)
                    except (IndexError, ValueError):
                        continue
                    out.setdefault((str(team.get("team_id")), stat), {})[str(player["id"])] = (
                        player.get("name", "").strip(), value, volume)
    return out


def prop_value(data, prop):
    """A prop's stat from a box score payload, or None if the player isn't in it."""
    athlete, stat = prop.split(":", 1)
    for (_, s), players in box_stats(data).items():
        if s == stat and athlete in players:
            return players[athlete][1]
    return None


def props(conn, league, game_id):
    """{"locked", "props": [...]} for a game: each team's main passer, rushers and receivers by volume over
    their last final box scores this season (last season's fill in when there are fewer than three), lined at
    their average yards rounded to a half point. None if the game doesn't exist."""
    game, locked = game_state(conn, league, game_id)
    if game is None:
        return None
    start = game["start_time"] or datetime.now(timezone.utc)
    out = []
    for team_id in (game["away_team_id"], game["home_team_id"]):
        history = conn.execute(
            "SELECT b.data, g.season FROM game_boxscores b JOIN games g USING (league, game_id) "
            "WHERE g.league = %s AND %s IN (g.home_team_id, g.away_team_id) AND b.final AND g.start_time < %s "
            "AND g.season IN (%s, %s) ORDER BY g.start_time DESC LIMIT 8",
            (league, team_id, start, game["season"], game["season"] - 1)).fetchall()
        this_season = [d for d, season in history if season == game["season"]]
        games = (this_season if len(this_season) >= 3 else [d for d, _ in history])[:5]
        parsed = [box_stats(d) for d in games]
        for stat, count in PROP_PLAYERS.items():
            seen = {}
            for box in parsed:
                for athlete, (name, value, volume) in box.get((str(team_id), stat), {}).items():
                    s = seen.setdefault(athlete, {"name": name, "values": [], "volume": 0.0})
                    s["values"].append(value)
                    s["volume"] += volume
            ranked = sorted((a for a in seen.items() if len(a[1]["values"]) >= 2), key=lambda a: -a[1]["volume"])
            for athlete, s in ranked[:count]:
                avg = mean(s["values"])
                if avg < 5:
                    continue
                label, short = PROP_STATS[stat][2], PROP_STATS[stat][3]
                out.append({"prop": f"{athlete}:{stat}", "player": s["name"], "team_id": str(team_id), "stat": stat,
                            "label": label, "name": f"{s['name']} · {short}", "line": math.floor(avg) + 0.5,
                            "over_price": PROP_PRICE, "under_price": PROP_PRICE,
                            "recent": [round(v) for v in s["values"]]})
    return {"locked": locked, "props": out}


def prop_quote(prop_list, prop, selection):
    """(line, price, name) for a prop side, or None if it isn't offered."""
    item = next((p for p in prop_list if p["prop"] == prop), None)
    if item is None or selection not in ("over", "under"):
        return None
    return item["line"], item[f"{selection}_price"], item["name"]


def win_amount(stake, price):
    """Profit on a winning bet at American odds."""
    stake = float(stake)
    return round(stake * price / 100 if price > 0 else stake * 100 / -price, 2)


def decimal_odds(price):
    """American odds as a decimal multiplier: -110 -> 1.909, +150 -> 2.5."""
    return 1 + (price / 100 if price > 0 else 100 / -price)


def american(decimal):
    """A decimal multiplier back to American odds (for showing a parlay's price)."""
    if decimal <= 1:
        return 0
    return round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))


def parlay_odds(prices):
    return math.prod(decimal_odds(p) for p in prices)


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


def confirm_live(conn):
    """Confirm in-game bets once the feed has refreshed after them with the score unchanged; void (refund)
    those where it changed, or where the feed never caught up. Returns how many were voided."""
    voided = 0
    for bet_id, placed_home, placed_away, home, away, refreshed, overdue in conn.execute(
            """
            SELECT b.id, b.placed_home, b.placed_away, l.home_score, l.away_score,
                   l.updated_at > b.placed_at + interval '10 seconds', b.pending_until < now() - interval '5 minutes'
            FROM bets b LEFT JOIN live_games l USING (league, game_id)
            WHERE b.live AND b.status = 'open' AND b.pending_until IS NOT NULL AND b.pending_until <= now()
            """).fetchall():
        if refreshed and (home, away) == (placed_home, placed_away):
            conn.execute("UPDATE bets SET pending_until = NULL WHERE id = %s", (bet_id,))
        elif refreshed or overdue:
            conn.execute("UPDATE bets SET status = 'void', profit = 0, settled_at = now(), notified = false "
                         "WHERE id = %s AND status = 'open'", (bet_id,))
            voided += 1
    return voided


def final_box(conn, league, game_id):
    row = conn.execute("SELECT data FROM game_boxscores WHERE league = %s AND game_id = %s AND final",
                       (league, game_id)).fetchone()
    return row[0] if row else None


def prop_outcome(value, selection, line):
    """won / lost / push for a prop, or void if the player didn't play."""
    if value is None:
        return "void"
    diff = value - float(line) if selection == "over" else float(line) - value
    return "won" if diff > 0 else "lost" if diff < 0 else "push"


def settle(conn):
    """Confirm in-game bets, then grade every open bet whose game is final (props once its final box score is
    in, or void after a day without one). Returns how many were graded."""
    graded = confirm_live(conn)
    for bet_id, league, game_id, prop, selection, line, price, stake, overdue in conn.execute(
            """
            SELECT b.id, b.league, b.game_id, b.prop, b.selection, b.line, b.price, b.stake,
                   g.start_time < now() - interval '1 day'
            FROM bets b JOIN games g USING (league, game_id)
            WHERE b.status = 'open' AND b.market = 'prop' AND g.completed
            """).fetchall():
        box = final_box(conn, league, game_id)
        if box is None and not overdue:
            continue
        status = prop_outcome(prop_value(box, prop) if box else None, selection, line)
        profit = win_amount(stake, price) if status == "won" else -float(stake) if status == "lost" else 0
        conn.execute("UPDATE bets SET status = %s, profit = %s, settled_at = now(), notified = false "
                     "WHERE id = %s AND status = 'open'", (status, profit, bet_id))
        graded += 1
    rows = conn.execute(
        """
        SELECT b.id, b.market, b.selection, b.line, b.price, b.stake, g.home_score, g.away_score
        FROM bets b JOIN games g USING (league, game_id)
        LEFT JOIN live_games l USING (league, game_id)
        WHERE b.status = 'open' AND b.market <> 'prop' AND b.pending_until IS NULL
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND (g.completed OR l.state = 'post')
        """).fetchall()
    for bet_id, market, selection, line, price, stake, home, away in rows:
        status = outcome(market, selection, line, home, away)
        profit = win_amount(stake, price) if status == "won" else -float(stake) if status == "lost" else 0
        conn.execute("UPDATE bets SET status = %s, profit = %s, settled_at = now(), notified = false "
                     "WHERE id = %s AND status = 'open'", (status, profit, bet_id))
    return graded + len(rows) + settle_parlays(conn)


def settle_parlays(conn):
    """Grade parlay legs whose games are final, then settle parlays that are decided: lost as soon as a leg
    loses, otherwise once every leg is graded. Returns how many parlays settled."""
    legs = conn.execute(
        """
        SELECT l.id, l.market, l.selection, l.line, g.home_score, g.away_score
        FROM parlay_legs l JOIN games g USING (league, game_id)
        LEFT JOIN live_games lg USING (league, game_id)
        WHERE l.status = 'open' AND l.market <> 'prop' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND (g.completed OR lg.state = 'post')
        """).fetchall()
    for leg_id, market, selection, line, home, away in legs:
        conn.execute("UPDATE parlay_legs SET status = %s WHERE id = %s",
                     (outcome(market, selection, line, home, away), leg_id))
    for leg_id, league, game_id, prop, selection, line, overdue in conn.execute(
            "SELECT l.id, l.league, l.game_id, l.prop, l.selection, l.line, g.start_time < now() - interval '1 day' "
            "FROM parlay_legs l JOIN games g USING (league, game_id) "
            "WHERE l.status = 'open' AND l.market = 'prop' AND g.completed").fetchall():
        box = final_box(conn, league, game_id)
        if box is not None or overdue:
            conn.execute("UPDATE parlay_legs SET status = %s WHERE id = %s",
                         (prop_outcome(prop_value(box, prop) if box else None, selection, line), leg_id))
    settled = 0
    for parlay_id, stake, statuses, prices in conn.execute(
            "SELECT p.id, p.stake, array_agg(l.status), array_agg(l.price) FROM parlays p "
            "JOIN parlay_legs l ON l.parlay_id = p.id WHERE p.status = 'open' GROUP BY p.id").fetchall():
        if "lost" in statuses:
            status, profit = "lost", -float(stake)
        elif "open" in statuses:
            continue
        elif all(s in ("push", "void") for s in statuses):
            status, profit = "push", 0
        else:
            won = [price for s, price in zip(statuses, prices) if s == "won"]
            status, profit = "won", round(float(stake) * parlay_odds(won) - float(stake), 2)
        conn.execute("UPDATE parlays SET status = %s, profit = %s, settled_at = now(), notified = false "
                     "WHERE id = %s AND status = 'open'", (status, profit, parlay_id))
        settled += 1
    return settled


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
    s = {"won": 0, "lost": 0, "push": 0, "open": 0, "void": 0, "profit": 0.0, "staked": 0.0,
         "tail": {"won": 0, "lost": 0, "push": 0}, "fade": {"won": 0, "lost": 0, "push": 0}}
    for status, stake, profit, selection, model_selection in bets:
        s[status] += 1
        if status in ("open", "void"):
            continue
        s["profit"] += float(profit or 0)
        s["staked"] += float(stake)
        if model_selection:
            s["tail" if model_selection == selection else "fade"][status] += 1
    s["profit"] = round(s["profit"], 2)
    s["roi"] = round(s["profit"] / s["staked"], 4) if s["staked"] else None
    return s


def bankroll(conn, player_id, reset_at):
    """(balance, available): units after graded bets and parlays since the last reset, and that minus open
    stakes."""
    graded, open_stakes = conn.execute(
        "SELECT COALESCE(sum(profit) FILTER (WHERE status <> 'open'), 0), "
        "COALESCE(sum(stake) FILTER (WHERE status = 'open'), 0) FROM ("
        "  SELECT status, stake, profit, placed_at FROM bets WHERE player_id = %(p)s"
        "  UNION ALL SELECT status, stake, profit, placed_at FROM parlays WHERE player_id = %(p)s) t "
        "WHERE %(r)s::timestamptz IS NULL OR placed_at > %(r)s::timestamptz",
        {"p": player_id, "r": reset_at}).fetchone()
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
