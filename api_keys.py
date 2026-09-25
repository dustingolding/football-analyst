"""Manage keys for the JSON API (/api/v1). Keys are shown once; only their SHA-256 hash is stored.

    python api_keys.py create "iOS app"            # prints the new key
    python api_keys.py create "load test" --rate 60
    python api_keys.py list
    python api_keys.py revoke <prefix>

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
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("prefix")
    args = parser.parse_args()

    with connect() as conn:
        init_db(conn)
        if args.command == "create":
            key = "slw_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO api_keys (key_hash, prefix, name, rate_per_minute) VALUES (%s, %s, %s, %s)",
                         (hashlib.sha256(key.encode()).hexdigest(), key[:12], args.name, args.rate))
            print(f"Created key for {args.name!r} ({args.rate}/min). Store it now; it can't be shown again:\n{key}")
        elif args.command == "list":
            for prefix, name, rate, active, created in conn.execute(
                "SELECT prefix, name, rate_per_minute, active, created_at FROM api_keys ORDER BY created_at"
            ):
                print(f"{prefix}...  {name:24s} {rate:5d}/min  {'active' if active else 'revoked'}  {created:%Y-%m-%d}")
        else:
            n = conn.execute("UPDATE api_keys SET active = false WHERE prefix = %s", (args.prefix,)).rowcount
            print(f"revoked {n} key(s)")


if __name__ == "__main__":
    main()
