"""Ads and SidelineWire Pro: the app's remote switches, and bonus units from rewarded ads.

GET /api/v1/config tells the app whether to show ads (and which AdMob ad units) and whether Pro is on sale.
Both are off until set in deploy/<env>.env, so a build never shows test ads or an unbuyable subscription.

Rewarded ads give Beat the Model bonus units (REWARD_UNITS each, REWARD_DAILY a day). Bonus units count in the
bankroll but not in profit, so the leaderboard can't be bought. Google confirms each watched ad by calling
/api/v1/admob/ssv (server-side verification): the query is signed with Google's published keys and carries
the player id we gave the ad as user_id. On dev, ADS_TRUST_CLIENT_REWARDS=1 also accepts the app's own claim,
for testing before an ad unit's SSV callback is set up in AdMob.
"""

import base64
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
REWARD_UNITS = int(os.getenv("ADS_REWARD_UNITS", "100"))
REWARD_DAILY = int(os.getenv("ADS_REWARD_DAILY", "3"))
FEED_EVERY = int(os.getenv("ADS_FEED_EVERY", "6"))
SSV_KEYS_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bet_bonuses (
    id              BIGSERIAL   PRIMARY KEY,
    player_id       TEXT        NOT NULL REFERENCES bet_players (player_id) ON DELETE CASCADE,
    units           NUMERIC     NOT NULL,
    source          TEXT        NOT NULL,                 -- rewarded_ad
    transaction_id  TEXT        UNIQUE,                   -- AdMob's, so a retried callback credits once
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bet_bonuses_player_idx ON bet_bonuses (player_id, created_at DESC);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_ro') THEN
        GRANT SELECT, INSERT ON bet_bonuses TO web_ro;
        GRANT USAGE ON SEQUENCE bet_bonuses_id_seq TO web_ro;
    END IF;
END $$;
"""


def flag(name):
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def config():
    """The app's remote switches."""
    units = {kind: os.getenv(f"ADMOB_{kind.upper()}_UNIT", "").strip() or None
             for kind in ("banner", "native", "rewarded")}
    return {
        "ads": {"enabled": flag("ADS_ENABLED"), "units": units, "feed_every": FEED_EVERY,
                "reward_units": REWARD_UNITS, "reward_daily": REWARD_DAILY},
        "pro": {"enabled": flag("PRO_ENABLED")},
    }


def start_of_day():
    now = datetime.now(timezone.utc).astimezone(EASTERN)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def claims_today(conn, player_id):
    return conn.execute("SELECT count(*) FROM bet_bonuses WHERE player_id = %s AND source = 'rewarded_ad' "
                        "AND created_at >= %s", (player_id, start_of_day())).fetchone()[0]


def credit_reward(conn, player_id, transaction_id):
    """Credit one rewarded ad: 'credited', 'duplicate' (already credited), 'limit' or 'unknown_player'."""
    if not conn.execute("SELECT 1 FROM bet_players WHERE player_id = %s FOR UPDATE", (player_id,)).fetchone():
        return "unknown_player"
    if conn.execute("SELECT 1 FROM bet_bonuses WHERE transaction_id = %s", (transaction_id,)).fetchone():
        return "duplicate"
    if claims_today(conn, player_id) >= REWARD_DAILY:
        return "limit"
    conn.execute("INSERT INTO bet_bonuses (player_id, units, source, transaction_id) VALUES (%s, %s, 'rewarded_ad', %s)",
                 (player_id, REWARD_UNITS, transaction_id))
    return "credited"


# --- AdMob server-side verification --------------------------------------------------------------

_keys = {"at": 0.0, "keys": {}}


def _verifier_keys(force=False):
    """Google's keys, cached a day. A forced refresh (an unknown key id: Google rotated) happens at most every
    10 minutes, so made-up key ids can't make us fetch on every request."""
    age = time.time() - _keys["at"]
    if age > 24 * 3600 or not _keys["keys"] or (force and age > 600):
        with urllib.request.urlopen(SSV_KEYS_URL, timeout=10) as response:  # noqa: S310 (fixed https URL)
            data = json.load(response)
        _keys["keys"] = {str(k["keyId"]): serialization.load_pem_public_key(k["pem"].encode())
                         for k in data.get("keys", [])}
        _keys["at"] = time.time()
    return _keys["keys"]


class SsvError(Exception):
    pass


def verify_ssv(query_string):
    """The callback's parameters if Google signed it; SsvError otherwise. The signed message is the query string
    up to "&signature=" (signature and key_id come last), signed with ECDSA P-256 / SHA-256."""
    marker = "&signature="
    if marker not in query_string:
        raise SsvError("unsigned")
    message = query_string[:query_string.index(marker)].encode()
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    signature, key_id = params.get("signature", ""), params.get("key_id", "")
    try:
        der = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except ValueError:
        raise SsvError("bad signature encoding") from None
    key = _verifier_keys().get(key_id) or _verifier_keys(force=True).get(key_id)
    if key is None:
        raise SsvError("unknown key")
    try:
        key.verify(der, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise SsvError("bad signature") from None
    return params
