"""Accounts: Sign in with Apple, app sessions and account deletion.

The app signs in with Apple and posts the identity token to POST /api/v1/auth/apple. We check it against
Apple's published keys (issuer, audience = our bundle id, expiry), find or create the user by Apple's stable
`sub`, and hand back an opaque session token the app sends as "Authorization: Bearer <token>". Only the
token's SHA-256 is stored.

Deleting an account (DELETE /api/v1/me) removes the user, their sessions, their mock-betting player and
their chat data. When a Sign in with Apple key is configured (APPLE_SIWA_KEY_P8, APPLE_SIWA_KEY_ID,
APPLE_TEAM_ID), sign-in also exchanges the authorization code for a refresh token so deletion can revoke
the app's access at Apple, as App Store review expects; without the key that step is skipped.
"""

import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import jwt

log = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
DEFAULT_BUNDLE_ID = "com.dustingoldingpersonalteam.footballanalyst"
DISPLAY_NAME_MAX = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id              TEXT        PRIMARY KEY,          -- random UUID
    apple_sub            TEXT        NOT NULL UNIQUE,      -- Apple's stable user id for this team
    display_name         TEXT        NOT NULL,
    email                TEXT,                             -- may be an Apple private relay address
    apple_refresh_token  TEXT,                             -- for revoking at Apple on deletion
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash    TEXT        PRIMARY KEY,                 -- sha256 of the bearer token
    user_id       TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    install_id    TEXT,                                    -- the app install (for chat alerts)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_sessions_user_idx ON user_sessions (user_id);

-- A signed-in player's bankroll follows the account to other devices.
ALTER TABLE bet_players ADD COLUMN IF NOT EXISTS user_id TEXT UNIQUE REFERENCES users (user_id) ON DELETE CASCADE;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_ro') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON users, user_sessions TO web_ro;
    END IF;
END $$;
"""

_jwks = jwt.PyJWKClient(APPLE_KEYS_URL, cache_keys=True, lifespan=24 * 3600)


class AuthError(Exception):
    """A sign-in the server won't accept; the message is safe to show."""


def bundle_ids():
    return [b.strip() for b in os.getenv("APPLE_BUNDLE_IDS", DEFAULT_BUNDLE_ID).split(",") if b.strip()]


def verify_identity_token(token):
    """Claims of a valid Apple identity token (sub, email, ...) for one of our apps; AuthError otherwise."""
    try:
        key = _jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=bundle_ids(), issuer=APPLE_ISSUER,
                          options={"require": ["sub", "exp", "iat"]})
    except jwt.PyJWKClientError as err:
        log.warning("apple keys unavailable: %s", err)
        raise AuthError("Couldn't reach Apple to check the sign-in. Try again.") from None
    except jwt.InvalidTokenError as err:
        raise AuthError(f"Apple sign-in wasn't valid ({err}).") from None


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean_display_name(name):
    name = " ".join(str(name or "").split())[:DISPLAY_NAME_MAX]
    return name or None


def sign_in(conn, claims, full_name=None, authorization_code=None, install_id=None):
    """Find or create the user for verified claims and open a session. Returns (token, user row dict)."""
    sub = claims["sub"]
    email = claims.get("email")
    row = conn.execute("SELECT user_id FROM users WHERE apple_sub = %s", (sub,)).fetchone()
    if row:
        user_id = row[0]
        if email:
            conn.execute("UPDATE users SET email = %s, updated_at = now() WHERE user_id = %s", (email, user_id))
    else:
        user_id = str(uuid.uuid4())
        # Apple sends the name only on the very first sign-in; otherwise start with a placeholder.
        name = clean_display_name(full_name) or f"Fan {secrets.randbelow(9000) + 1000}"
        conn.execute("INSERT INTO users (user_id, apple_sub, display_name, email) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT (apple_sub) DO NOTHING", (user_id, sub, name, email))
        user_id = conn.execute("SELECT user_id FROM users WHERE apple_sub = %s", (sub,)).fetchone()[0]
    if authorization_code:
        refresh = exchange_code(authorization_code)
        if refresh:
            conn.execute("UPDATE users SET apple_refresh_token = %s WHERE user_id = %s", (refresh, user_id))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO user_sessions (token_hash, user_id, install_id) VALUES (%s, %s, %s)",
                 (hash_token(token), user_id, install_id))
    return token, user(conn, user_id)


def user(conn, user_id):
    row = conn.execute("SELECT user_id, display_name, email, created_at FROM users WHERE user_id = %s",
                       (user_id,)).fetchone()
    return dict(zip(("user_id", "display_name", "email", "created_at"), row)) if row else None


def session_user(conn, token):
    """The user id for a bearer token, or None. Refreshes last_seen at most every 10 minutes."""
    if not token:
        return None
    row = conn.execute(
        "UPDATE user_sessions SET last_seen_at = now() WHERE token_hash = %s "
        "AND last_seen_at < now() - interval '10 minutes' RETURNING user_id", (hash_token(token),)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT user_id FROM user_sessions WHERE token_hash = %s", (hash_token(token),)).fetchone()
    return row[0] if row else None


def link_player(conn, user_id, player_id):
    """The mock-betting player for this account: the one already linked, else this device's (if unclaimed).
    Returns the player id to use, or None if there isn't one yet."""
    row = conn.execute("SELECT player_id FROM bet_players WHERE user_id = %s", (user_id,)).fetchone()
    if row:
        return row[0]
    if player_id:
        row = conn.execute("UPDATE bet_players SET user_id = %s, updated_at = now() "
                           "WHERE player_id = %s AND user_id IS NULL RETURNING player_id",
                           (user_id, player_id)).fetchone()
        if row:
            return row[0]
    return None


def delete_user(conn, user_id):
    """Revoke at Apple (when possible) and delete the account and everything tied to it."""
    row = conn.execute("SELECT apple_refresh_token FROM users WHERE user_id = %s", (user_id,)).fetchone()
    if row and row[0]:
        revoke(row[0])
    conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))  # everything tied to the user cascades


# --- Apple REST (only with a Sign in with Apple key) ---------------------------------------------

def _siwa_config():
    key, key_id, team = os.getenv("APPLE_SIWA_KEY_P8"), os.getenv("APPLE_SIWA_KEY_ID"), os.getenv("APPLE_TEAM_ID")
    return (key.replace("\\n", "\n"), key_id, team) if key and key_id and team else None


def _client_secret(config):
    key, key_id, team = config
    now = int(time.time())
    return jwt.encode({"iss": team, "iat": now, "exp": now + 300, "aud": APPLE_ISSUER, "sub": bundle_ids()[0]},
                      key, algorithm="ES256", headers={"kid": key_id})


def _post(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(APPLE_ISSUER + path, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (fixed https URL)
        body = response.read()
    return json.loads(body) if body else {}


def exchange_code(code):
    """Refresh token for an authorization code, or None (no key configured, or Apple refused)."""
    config = _siwa_config()
    if not config:
        return None
    try:
        return _post("/auth/token", {"client_id": bundle_ids()[0], "client_secret": _client_secret(config),
                                     "code": code, "grant_type": "authorization_code"}).get("refresh_token")
    except (urllib.error.URLError, ValueError) as err:
        log.warning("apple code exchange failed: %s", err)
        return None


def revoke(refresh_token):
    config = _siwa_config()
    if not config:
        return
    try:
        _post("/auth/revoke", {"client_id": bundle_ids()[0], "client_secret": _client_secret(config),
                               "token": refresh_token, "token_type_hint": "refresh_token"})
    except (urllib.error.URLError, ValueError) as err:
        log.warning("apple revoke failed: %s", err)
