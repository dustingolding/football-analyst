"""Manage keys for the JSON API (/api/v1). Keys are shown once; only their SHA-256 hash is stored.

    python api_keys.py create "iOS app"            # prints the new key
    python api_keys.py create "load test" --rate 60
    python api_keys.py create "iOS bootstrap" --scope bootstrap --rate 600   # built into the app
    python api_keys.py list
    python api_keys.py revoke <prefix>
    python api_keys.py installs                    # install tokens in use
    python api_keys.py revoke-install <install_id> # cut off one app install (it can ask for a new token)

A bootstrap key can only mint install tokens (POST /api/v1/installs); the app then uses its own token.

Uses DATABASE_URL (run as ./kenv dev ... for football, ./kenv prod ... for football_prod), so create
keys separately for each environment.
"""

import argparse
import hashlib
import secrets

from database import connect, init_db


def main():
    parser = argparse.ArgumentParser(description="Manage JSON API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--rate", type=int, default=300, help="requests per minute (default 300)")
    create.add_argument("--scope", choices=("full", "bootstrap"), default="full",
                        help="full API, or only minting install tokens (default full)")
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("prefix")
    sub.add_parser("installs")
    revoke_install = sub.add_parser("revoke-install")
    revoke_install.add_argument("install_id")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if args.command == "create":
            key = "slw_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO api_keys (key_hash, prefix, name, rate_per_minute, scope) VALUES (%s, %s, %s, %s, %s)",
                         (hashlib.sha256(key.encode()).hexdigest(), key[:12], args.name, args.rate, args.scope))
            print(f"Created {args.scope} key for {args.name!r} ({args.rate}/min). Store it now; it can't be shown "
                  f"again:\n{key}")
        elif args.command == "list":
            for prefix, name, rate, scope, active, created in conn.execute(
                "SELECT prefix, name, rate_per_minute, scope, active, created_at FROM api_keys ORDER BY created_at"
            ):
                print(f"{prefix}...  {name:24s} {scope:9s} {rate:5d}/min  {'active' if active else 'revoked'}  "
                      f"{created:%Y-%m-%d}")
        elif args.command == "installs":
            for install_id, tokens, last_seen, version in conn.execute(
                "SELECT install_id, count(*), max(last_seen_at), max(app_version) FROM install_tokens "
                "WHERE revoked_at IS NULL GROUP BY install_id ORDER BY 3 DESC"
            ):
                print(f"{install_id}  {tokens} token(s)  last seen {last_seen:%Y-%m-%d %H:%M}  {version or ''}")
        elif args.command == "revoke-install":
            n = conn.execute("UPDATE install_tokens SET revoked_at = now() WHERE install_id = %s AND revoked_at IS NULL",
                             (args.install_id.lower(),)).rowcount
            print(f"revoked {n} token(s)")
        else:
            n = conn.execute("UPDATE api_keys SET active = false WHERE prefix = %s", (args.prefix,)).rowcount
            print(f"revoked {n} key(s)")


if __name__ == "__main__":
    main()
