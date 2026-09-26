"""Private group chat: invite-only groups of signed-in users, text messages (optionally sharing a game),
reporting, blocking, and push alerts for new messages.

Groups are joined with an 8-character invite code. The owner can rename the group, rotate the code, remove
members and delete any message; everyone can leave, delete their own messages, report messages and block
users (a blocked user's messages are hidden from the blocker and don't alert them). Words listed in
CHAT_BLOCKLIST (comma-separated) are masked when a message is posted. notify.py calls push_pass every pass.
"""

import os
import re
import secrets
import uuid

GROUP_NAME_MAX = 40
MESSAGE_MAX = 1000
MAX_MEMBERS = 50
MAX_GROUPS = 20                # per user
RATE_PER_MINUTE = 30           # messages per user
PUSH_WINDOW = "10 minutes"     # older unsent messages are marked sent without alerting
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_groups (
    group_id     TEXT        PRIMARY KEY,
    name         TEXT        NOT NULL,
    owner_id     TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    invite_code  TEXT        NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_members (
    group_id      TEXT        NOT NULL REFERENCES chat_groups (group_id) ON DELETE CASCADE,
    user_id       TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    muted         BOOLEAN     NOT NULL DEFAULT false,
    last_read_id  BIGINT      NOT NULL DEFAULT 0,
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS chat_members_user_idx ON chat_members (user_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL   PRIMARY KEY,
    group_id    TEXT        NOT NULL REFERENCES chat_groups (group_id) ON DELETE CASCADE,
    user_id     TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    body        TEXT        NOT NULL,
    league      TEXT,                          -- a shared game, if any
    game_id     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,
    pushed      BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS chat_messages_group_idx ON chat_messages (group_id, id DESC);
CREATE INDEX IF NOT EXISTS chat_messages_unpushed_idx ON chat_messages (id) WHERE NOT pushed;
CREATE INDEX IF NOT EXISTS chat_messages_user_idx ON chat_messages (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_reports (
    id           BIGSERIAL   PRIMARY KEY,
    message_id   BIGINT      NOT NULL REFERENCES chat_messages (id) ON DELETE CASCADE,
    reporter_id  TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    reason       TEXT,
    body         TEXT        NOT NULL,        -- the message as reported
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at  TIMESTAMPTZ,
    UNIQUE (message_id, reporter_id)
);

CREATE TABLE IF NOT EXISTS user_blocks (
    blocker_id  TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    blocked_id  TEXT        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (blocker_id, blocked_id)
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_ro') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON chat_groups, chat_members, chat_messages, chat_reports,
            user_blocks TO web_ro;
        GRANT USAGE ON SEQUENCE chat_messages_id_seq, chat_reports_id_seq TO web_ro;
    END IF;
END $$;
"""


class ChatError(Exception):
    """A request the rules refuse: (status, code, message) for the API."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def clean_name(name):
    name = " ".join(str(name or "").split())[:GROUP_NAME_MAX]
    if not name:
        raise ChatError(400, "bad_request", f"Group names are 1-{GROUP_NAME_MAX} characters.")
    return name


def _blocklist():
    return [w.strip().lower() for w in os.getenv("CHAT_BLOCKLIST", "").split(",") if w.strip()]


def filter_text(text):
    """Mask blocklisted words (whole words, any case) with asterisks."""
    for word in _blocklist():
        text = re.sub(rf"\b{re.escape(word)}\b", lambda m: "*" * len(m.group()), text, flags=re.IGNORECASE)
    return text


def clean_body(body):
    body = str(body or "").strip()
    if not body or len(body) > MESSAGE_MAX:
        raise ChatError(400, "bad_request", f"Messages are 1-{MESSAGE_MAX} characters.")
    return filter_text(body)


# --- groups --------------------------------------------------------------------------------------

def membership(conn, group_id, user_id):
    """(owner_id) of a group the user belongs to; 404 otherwise (not revealing whether the group exists)."""
    row = conn.execute(
        "SELECT g.owner_id FROM chat_groups g JOIN chat_members m USING (group_id) "
        "WHERE g.group_id = %s AND m.user_id = %s", (group_id, user_id)).fetchone()
    if not row:
        raise ChatError(404, "not_found", "No such group.")
    return row[0]


def require_owner(conn, group_id, user_id):
    if membership(conn, group_id, user_id) != user_id:
        raise ChatError(403, "forbidden", "Only the group's creator can do that.")


def _check_group_count(conn, user_id):
    count = conn.execute("SELECT count(*) FROM chat_members WHERE user_id = %s", (user_id,)).fetchone()[0]
    if count >= MAX_GROUPS:
        raise ChatError(409, "limit", f"You can be in up to {MAX_GROUPS} groups.")


def create_group(conn, user_id, name):
    name = clean_name(name)
    _check_group_count(conn, user_id)
    group_id = str(uuid.uuid4())
    for _ in range(5):  # codes are random; retry the rare collision
        code = new_code()
        if conn.execute("INSERT INTO chat_groups (group_id, name, owner_id, invite_code) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (invite_code) DO NOTHING RETURNING 1", (group_id, name, user_id, code)).fetchone():
            break
    else:
        raise ChatError(503, "unavailable", "Couldn't create the group; try again.")
    conn.execute("INSERT INTO chat_members (group_id, user_id) VALUES (%s, %s)", (group_id, user_id))
    return group_id


def join_group(conn, user_id, code):
    code = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    row = conn.execute("SELECT group_id FROM chat_groups WHERE invite_code = %s FOR UPDATE", (code,)).fetchone()
    if not row:
        raise ChatError(404, "not_found", "That invite code doesn't match a group. Check it and try again.")
    group_id = row[0]
    if conn.execute("SELECT 1 FROM chat_members WHERE group_id = %s AND user_id = %s", (group_id, user_id)).fetchone():
        return group_id
    _check_group_count(conn, user_id)
    members = conn.execute("SELECT count(*) FROM chat_members WHERE group_id = %s", (group_id,)).fetchone()[0]
    if members >= MAX_MEMBERS:
        raise ChatError(409, "full", f"That group is full ({MAX_MEMBERS} members).")
    # Start "read" at the latest message so joining doesn't show a pile of unread history.
    latest = conn.execute("SELECT COALESCE(max(id), 0) FROM chat_messages WHERE group_id = %s", (group_id,)).fetchone()[0]
    conn.execute("INSERT INTO chat_members (group_id, user_id, last_read_id) VALUES (%s, %s, %s)",
                 (group_id, user_id, latest))
    return group_id


def rotate_code(conn, group_id, user_id):
    require_owner(conn, group_id, user_id)
    for _ in range(5):
        code = new_code()
        if conn.execute("UPDATE chat_groups SET invite_code = %s, updated_at = now() WHERE group_id = %s "
                        "AND NOT EXISTS (SELECT 1 FROM chat_groups WHERE invite_code = %s) RETURNING 1",
                        (code, group_id, code)).fetchone():
            return code
    raise ChatError(503, "unavailable", "Couldn't make a new code; try again.")


def remove_member(conn, group_id, user_id, member_id):
    """Leave (member_id is the caller) or, for the owner, remove someone. An owner who leaves hands the group to
    the longest-standing member, or deletes it if nobody's left."""
    owner = membership(conn, group_id, user_id)
    if member_id != user_id and owner != user_id:
        raise ChatError(403, "forbidden", "Only the group's creator can remove members.")
    conn.execute("DELETE FROM chat_members WHERE group_id = %s AND user_id = %s", (group_id, member_id))
    if member_id == owner:
        heir = conn.execute("SELECT user_id FROM chat_members WHERE group_id = %s ORDER BY joined_at LIMIT 1",
                            (group_id,)).fetchone()
        if heir:
            conn.execute("UPDATE chat_groups SET owner_id = %s, updated_at = now() WHERE group_id = %s",
                         (heir[0], group_id))
        else:
            conn.execute("DELETE FROM chat_groups WHERE group_id = %s", (group_id,))


def groups_for(conn, user_id):
    """The user's groups, most recently active first, with unread counts and the last visible message."""
    rows = conn.execute(
        """
        SELECT g.group_id, g.name, g.owner_id, g.invite_code, m.muted, m.last_read_id,
               (SELECT count(*) FROM chat_members x WHERE x.group_id = g.group_id) AS members,
               (SELECT count(*) FROM chat_messages c WHERE c.group_id = g.group_id AND c.id > m.last_read_id
                  AND c.user_id <> %(me)s AND c.deleted_at IS NULL
                  AND c.user_id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = %(me)s)) AS unread,
               last.id, last.body, last.created_at, last.user_id, u.display_name, g.created_at
        FROM chat_members m JOIN chat_groups g USING (group_id)
        LEFT JOIN LATERAL (
            SELECT c.id, c.body, c.created_at, c.user_id FROM chat_messages c
            WHERE c.group_id = g.group_id AND c.deleted_at IS NULL
              AND c.user_id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = %(me)s)
            ORDER BY c.id DESC LIMIT 1) last ON true
        LEFT JOIN users u ON u.user_id = last.user_id
        WHERE m.user_id = %(me)s
        ORDER BY COALESCE(last.created_at, g.created_at) DESC
        """, {"me": user_id}).fetchall()
    keys = ("group_id", "name", "owner_id", "invite_code", "muted", "last_read_id", "members", "unread",
            "last_id", "last_body", "last_at", "last_user_id", "last_name", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def members_of(conn, group_id, user_id):
    rows = conn.execute(
        """
        SELECT u.user_id, u.display_name, m.joined_at,
               EXISTS (SELECT 1 FROM user_blocks b WHERE b.blocker_id = %s AND b.blocked_id = u.user_id)
        FROM chat_members m JOIN users u USING (user_id) WHERE m.group_id = %s ORDER BY m.joined_at
        """, (user_id, group_id)).fetchall()
    return [dict(zip(("user_id", "display_name", "joined_at", "blocked"), r)) for r in rows]


# --- messages ------------------------------------------------------------------------------------

MESSAGE_COLUMNS = "c.id, c.user_id, u.display_name, c.body, c.league, c.game_id, c.created_at, c.deleted_at"
MESSAGE_KEYS = ("id", "user_id", "display_name", "body", "league", "game_id", "created_at", "deleted_at")


def messages(conn, group_id, user_id, after=None, before=None, limit=50):
    """Messages oldest first: those after an id (polling), before an id (scrolling back), or the latest."""
    params = {"g": group_id, "me": user_id, "after": after, "before": before, "limit": limit}
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT {MESSAGE_COLUMNS} FROM chat_messages c JOIN users u USING (user_id)
            WHERE c.group_id = %(g)s
              AND c.user_id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = %(me)s)
              AND (%(after)s::bigint IS NULL OR c.id > %(after)s::bigint)
              AND (%(before)s::bigint IS NULL OR c.id < %(before)s::bigint)
            ORDER BY c.id {"ASC" if after is not None else "DESC"} LIMIT %(limit)s
        ) page ORDER BY id
        """, params).fetchall()
    return [dict(zip(MESSAGE_KEYS, r)) for r in rows]


def post_message(conn, group_id, user_id, body, league=None, game_id=None):
    membership(conn, group_id, user_id)
    body = clean_body(body)
    recent = conn.execute("SELECT count(*) FROM chat_messages WHERE user_id = %s AND created_at > now() - "
                          "interval '1 minute'", (user_id,)).fetchone()[0]
    if recent >= RATE_PER_MINUTE:
        raise ChatError(429, "rate_limited", "You're sending messages too fast. Wait a moment.")
    if game_id and not conn.execute("SELECT 1 FROM games WHERE league = %s AND game_id = %s",
                                    (league, game_id)).fetchone():
        league = game_id = None
    row = conn.execute(
        f"WITH c AS (INSERT INTO chat_messages (group_id, user_id, body, league, game_id) VALUES (%s, %s, %s, %s, %s) "
        f"RETURNING *) SELECT {MESSAGE_COLUMNS} FROM c JOIN users u USING (user_id)",
        (group_id, user_id, body, league if game_id else None, game_id or None)).fetchone()
    # Sending counts as reading everything up to here.
    conn.execute("UPDATE chat_members SET last_read_id = GREATEST(last_read_id, %s) WHERE group_id = %s "
                 "AND user_id = %s", (row[0], group_id, user_id))
    return dict(zip(MESSAGE_KEYS, row))


def delete_message(conn, group_id, user_id, message_id):
    owner = membership(conn, group_id, user_id)
    row = conn.execute("SELECT user_id FROM chat_messages WHERE id = %s AND group_id = %s AND deleted_at IS NULL",
                       (message_id, group_id)).fetchone()
    if not row:
        raise ChatError(404, "not_found", "No such message.")
    if row[0] != user_id and owner != user_id:
        raise ChatError(403, "forbidden", "You can delete your own messages; the group's creator can delete any.")
    conn.execute("UPDATE chat_messages SET deleted_at = now(), body = '' WHERE id = %s", (message_id,))


def report_message(conn, group_id, user_id, message_id, reason):
    membership(conn, group_id, user_id)
    row = conn.execute("SELECT body, user_id FROM chat_messages WHERE id = %s AND group_id = %s",
                       (message_id, group_id)).fetchone()
    if not row:
        raise ChatError(404, "not_found", "No such message.")
    if row[1] == user_id:
        raise ChatError(400, "bad_request", "You can't report your own message.")
    conn.execute("INSERT INTO chat_reports (message_id, reporter_id, reason, body) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (message_id, reporter_id) DO NOTHING",
                 (message_id, user_id, str(reason or "")[:200] or None, row[0]))


def mark_read(conn, group_id, user_id, last_read_id):
    membership(conn, group_id, user_id)
    conn.execute("UPDATE chat_members SET last_read_id = GREATEST(last_read_id, %s) WHERE group_id = %s "
                 "AND user_id = %s", (int(last_read_id), group_id, user_id))


def set_muted(conn, group_id, user_id, muted):
    membership(conn, group_id, user_id)
    conn.execute("UPDATE chat_members SET muted = %s WHERE group_id = %s AND user_id = %s",
                 (bool(muted), group_id, user_id))


def block(conn, user_id, blocked_id, on=True):
    if blocked_id == user_id:
        raise ChatError(400, "bad_request", "You can't block yourself.")
    if on:
        if not conn.execute("SELECT 1 FROM users WHERE user_id = %s", (blocked_id,)).fetchone():
            raise ChatError(404, "not_found", "No such user.")
        conn.execute("INSERT INTO user_blocks (blocker_id, blocked_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (user_id, blocked_id))
    else:
        conn.execute("DELETE FROM user_blocks WHERE blocker_id = %s AND blocked_id = %s", (user_id, blocked_id))


# --- push (notify.py) ----------------------------------------------------------------------------

PENDING = f"""
SELECT c.id, c.group_id, c.user_id, c.body, g.name, u.display_name, c.game_id IS NOT NULL
FROM chat_messages c JOIN chat_groups g USING (group_id) JOIN users u ON u.user_id = c.user_id
WHERE NOT c.pushed AND c.deleted_at IS NULL AND c.created_at > now() - interval '{PUSH_WINDOW}'
ORDER BY c.id LIMIT 200
"""

RECIPIENTS = """
SELECT DISTINCT d.install_id, d.apns_token, d.environment, d.bundle_id
FROM chat_members m
JOIN user_sessions s ON s.user_id = m.user_id
JOIN push_devices d ON d.install_id = s.install_id AND d.disabled_at IS NULL
WHERE m.group_id = %(g)s AND m.user_id <> %(sender)s AND NOT m.muted
  AND NOT EXISTS (SELECT 1 FROM user_blocks b WHERE b.blocker_id = m.user_id AND b.blocked_id = %(sender)s)
"""


def push_pass(conn, apns, dry_run=False):
    """Alert group members about new messages (one alert per message per device, threaded by group). Returns
    how many messages were handled."""
    pending = conn.execute(PENDING).fetchall()
    for message_id, group_id, sender, body, group_name, sender_name, shared_game in pending:
        text = body if len(body) <= 180 else body[:177] + "…"
        alert = {"title": group_name, "body": f"{sender_name}: {text or ('shared a game' if shared_game else '')}"}
        devices = conn.execute(RECIPIENTS, {"g": group_id, "sender": sender}).fetchall()
        if dry_run:
            print(f"[notify] dry run: chat {message_id} -> {len(devices)} device(s): {alert}", flush=True)
            continue
        failed = 0
        for install_id, token, environment, bundle_id in devices:
            if apns is None:
                print(f"[notify] (no APNs key) would send chat {message_id} to {install_id}", flush=True)
                continue
            result = apns.send(token, environment, bundle_id, alert, data={"kind": "chat", "group_id": group_id},
                               thread_id=f"chat-{group_id}", expires_in=3600)
            if not result.ok:
                failed += 1
                if result.dead_token:
                    conn.execute("UPDATE push_devices SET disabled_at = now(), last_error = %s WHERE install_id = %s",
                                 (result.reason or str(result.status), install_id))
        conn.execute("UPDATE chat_messages SET pushed = true WHERE id = %s", (message_id,))
        print(f"[notify] chat {message_id} in {group_id}: {len(devices) - failed} sent, {failed} failed", flush=True)
    if not dry_run:
        # Anything older than the window (say, while the notifier was down) is stale news: don't alert.
        conn.execute(f"UPDATE chat_messages SET pushed = true WHERE NOT pushed AND created_at <= now() - "
                     f"interval '{PUSH_WINDOW}'")
    return len(pending)
