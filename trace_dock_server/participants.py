"""Controlled participant provisioning: prints each new token exactly once."""
import argparse
import hashlib
import hmac
import json
import secrets
import sys
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import Database
from .models import Participant, User
from .service import now
from .settings import get_settings
from .token_security import issue_token


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
    with db.session() as session:
        if not db.sqlite:
            lock_key = int.from_bytes(hashlib.sha256(args.email.encode()).digest()[:8], "big", signed=True)
            session.execute(select(func.pg_advisory_xact_lock(lock_key)))
        rows = session.scalars(select(Participant).where(Participant.email == args.email)).all()
        if args.action == "revoke":
            if not rows:
                parser.error("participant not found")
            revoked_at = now()
            for row in rows:
                if row.status == "active":
                    row.status = "revoked"
                    row.revoked_at = revoked_at
            print("revoked")
            return
        token = issue_token(args.email, pepper)
        token_hash = hmac.new(pepper.encode(), token.encode(), hashlib.sha256).digest()
        issued_at = now()
        insert = sqlite_insert if db.sqlite else pg_insert
        session.execute(insert(User).values(
            email=args.email, created_at=issued_at, updated_at=issued_at
        ).on_conflict_do_nothing(index_elements=[User.email]))
        participant = secrets.token_hex(16)
        session.add(Participant(
            participant_id=participant,
            email=args.email,
            token_hash=token_hash,
            status="active",
            created_at=issued_at,
            updated_at=issued_at,
        ))
    print(json.dumps({"participant_id": participant, "email": args.email, "token": token}))
    print("Token shown once. Each issue creates an additional active token; revoke by email revokes all of them.", file=sys.stderr)


if __name__ == "__main__":
    main()
