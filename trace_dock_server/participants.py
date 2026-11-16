"""Controlled participant provisioning: prints each new token exactly once."""
import argparse
import hashlib
import hmac
import json
import secrets
import sys
from .db import Database
from .service import now
from .settings import get_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("issue", "revoke"))
    parser.add_argument("email")
    args = parser.parse_args()
    if "@" not in args.email or any(c.isspace() for c in args.email):
        parser.error("invalid email")
    settings = get_settings()
    pepper = settings.token_pepper.get_secret_value()
    db = Database(settings.database_url.get_secret_value(), allow_sqlite=settings.trace_dock_dev)
    db.initialize()
    with db.connect() as conn:
        row = conn.one("SELECT * FROM participants WHERE email=?", (args.email,), lock=True)
        if args.action == "revoke":
            if not row:
                parser.error("participant not found")
            conn.execute("UPDATE participants SET status='revoked',revoked_at=? WHERE email=?", (now(), args.email))
            print("revoked")
            return
        token = secrets.token_urlsafe(32)
        token_hash = hmac.new(pepper.encode(), token.encode(), hashlib.sha256).digest()
        participant = row["participant_id"] if row else secrets.token_hex(16)
        conn.execute("""INSERT INTO participants(participant_id,email,token_hash,status,created_at)
            VALUES(?,?,?,'active',?) ON CONFLICT(email) DO UPDATE SET token_hash=excluded.token_hash,status='active',revoked_at=NULL""", (participant, args.email, token_hash, now()))
    print(json.dumps({"participant_id": participant, "email": args.email, "token": token}))
    print("Token shown once. Deliver securely; issuing again revokes the previous token.", file=sys.stderr)


if __name__ == "__main__":
    main()
