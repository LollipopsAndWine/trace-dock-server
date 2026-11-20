from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trace_dock_server.db import Database
from trace_dock_server.models import Collection, Participant, User


CREATED = "2000-01-01T00:00:00+00:00"
CONSENTED = "2000-01-02T00:00:00+00:00"
CONFIRMED = "2000-01-03T00:00:00+00:00"
COMPLETED = "2000-01-04T00:00:00+00:00"


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


class DatabaseTimestampTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.db"
        self.url = "sqlite:///" + str(self.path)
        self.db = Database(self.url, allow_sqlite=True)

    def insert_rows(self):
        with self.db.session() as session:
            session.add(User(email="test@example.com", created_at=CREATED))
            session.flush()
            session.add(Participant(
                participant_id="participant", email="test@example.com", token_hash=b"test-token-hash",
                status="active", created_at=CREATED,
            ))
            session.flush()
            session.add(Collection(
                job_id="job", participant_id="participant", device_id="device", machine_name="machine",
                client_operation_id="operation", create_fingerprint="fingerprint", rules_json="{}",
                state="created", tool="codex", created_at=CREATED,
            ))

    @contextmanager
    def sqlite_connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_row(self, table):
        with self.sqlite_connection() as db:
            return dict(db.execute(f"SELECT * FROM {table}").fetchone())

    def test_inserts_updates_noops_and_rollbacks(self):
        self.db.initialize()
        self.insert_rows()
        user = self.read_row("users")
        self.assertIn("updated_at", user)
        self.assertEqual(parse_time(user["updated_at"]), parse_time(user["created_at"]))
        for table in ("participants", "collections"):
            with self.subTest(table=table):
                row = self.read_row(table)
                self.assertEqual(
                    parse_time(row["updated_at"]),
                    parse_time(row["created_at"]),
                )
                with self.db.session() as session:
                    if table == "participants":
                        participant = session.get(Participant, "participant")
                        participant.status, participant.revoked_at = "revoked", COMPLETED
                    else:
                        collection = session.get(Collection, "job")
                        collection.state, collection.selected_count = "collecting", 1
                changed = self.read_row(table)
                self.assertEqual(
                    parse_time(changed["created_at"]), parse_time(CREATED),
                )
                modified = parse_time(changed["updated_at"])
                self.assertGreater(modified, datetime.fromisoformat(CREATED))
                self.assertEqual(modified.utcoffset(), timezone.utc.utcoffset(None))
                with self.db.session() as session:
                    model = session.get(Participant if table == "participants" else Collection,
                                        "participant" if table == "participants" else "job")
                    if table == "participants":
                        model.status = model.status
                    else:
                        model.state = model.state
                self.assertEqual(self.read_row(table)["updated_at"], changed["updated_at"])
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with self.db.session() as session:
                        if table == "participants":
                            participant = session.get(Participant, "participant")
                            participant.status, participant.revoked_at = "active", None
                        else:
                            session.get(Collection, "job").state = "cancelled"
                        raise RuntimeError("rollback")
                self.assertEqual(self.read_row(table), changed)
        with self.sqlite_connection() as db:
            for table in ("users", "participants", "collections"):
                columns = {r["name"]: r for r in db.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(columns["updated_at"]["notnull"], 1)

    def test_existing_database_backfill_is_repeatable_and_preserves_rows(self):
        # The pre-upgrade schema has no updated_at columns or timestamp triggers.
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE participants (
                    participant_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    token_hash BLOB NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, revoked_at TEXT);
                CREATE TABLE collections (
                    job_id TEXT PRIMARY KEY, participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    state TEXT NOT NULL, tool TEXT NOT NULL, created_at TEXT NOT NULL,
                    consented_at TEXT, upload_confirmed_at TEXT, completed_at TEXT,
                    manifest_key TEXT, manifest_sha256 BLOB, uploaded_bytes BIGINT DEFAULT 0);
                """)
            conn.execute("INSERT INTO participants VALUES('active','active@example.com',?,'active',?,NULL)", (b"hash-one", CREATED))
            conn.execute("INSERT INTO participants VALUES('revoked','revoked@example.com',?,'revoked',?,?)", (b"hash-two", CREATED, COMPLETED))
            for job, state, consent, confirmed, completed in (
                ("new", "created", None, None, None),
                ("collect", "collecting", CONSENTED, None, None),
                ("upload", "uploading", CONSENTED, CONFIRMED, None),
                ("done", "completed", CONSENTED, CONFIRMED, COMPLETED),
            ):
                conn.execute("INSERT INTO collections VALUES(?,'active',?,'codex',?,?,?,?,?, ?,123)",
                             (job, state, CREATED, consent, confirmed, completed, "object/manifest.json", b"manifest-hash"))
            conn.row_factory = sqlite3.Row
            before = {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                      for table in ("participants", "collections")}
        self.db.initialize()
        with self.sqlite_connection() as db:
            for table in ("participants", "collections"):
                after = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                self.assertEqual([{k: v for k, v in r.items() if k not in ("updated_at", "manifest_crc64")} for r in after], before[table])
                self.assertTrue(all(r["updated_at"] for r in after))
                self.assertEqual(next(r for r in db.execute(f"PRAGMA table_info({table})") if r["name"] == "updated_at")["notnull"], 1)
            participant_times = {r["participant_id"]: r["updated_at"] for r in db.execute("SELECT * FROM participants")}
            job_times = {r["job_id"]: r["updated_at"] for r in db.execute("SELECT * FROM collections")}
            self.assertEqual(participant_times, {"active": CREATED, "revoked": COMPLETED})
            self.assertEqual(job_times, {"new": CREATED, "collect": CONSENTED, "upload": CONFIRMED, "done": COMPLETED})
            before_reinitialize = [dict(r) for r in db.execute("SELECT * FROM collections ORDER BY 1")]
        self.db.initialize()
        with self.sqlite_connection() as db:
            self.assertEqual([dict(r) for r in db.execute("SELECT * FROM collections ORDER BY 1")], before_reinitialize)
            db.execute("UPDATE collections SET state='cancelled' WHERE job_id='new'")
            updated = dict(db.execute("SELECT * FROM collections WHERE job_id='new'").fetchone())
            self.assertEqual(updated["state"], "cancelled")
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

    def test_new_migration_drops_trigger_without_rebuilding_participants(self):
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE users (email TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE participants (
                    participant_id TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email),
                    token_hash BLOB NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, revoked_at TEXT);
                CREATE TRIGGER participants_updated_at_update
                AFTER UPDATE ON participants
                BEGIN
                    UPDATE participants SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE participant_id = NEW.participant_id;
                END;
                """)
            conn.execute("INSERT INTO users VALUES('person@example.com', ?)", (CREATED,))
            conn.execute("INSERT INTO participants VALUES('participant','person@example.com',?,'active',?,NULL)", (b"hash", CREATED))

        self.db.initialize()
        with self.sqlite_connection() as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name='participants_updated_at_update'"
            ).fetchone())
        with self.db.session() as session:
            participant = session.get(Participant, "participant")
            participant.status = "revoked"
            session.flush()
            expected_updated_at = participant.updated_at
        actual_updated_at = self.read_row("participants")["updated_at"]
        expected_value = expected_updated_at.isoformat() if isinstance(expected_updated_at, datetime) else expected_updated_at
        self.assertEqual(parse_time(actual_updated_at), parse_time(expected_value))

    def test_parallel_initialization_does_not_duplicate_migrations(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Database(self.url, allow_sqlite=True).initialize(), range(4)))
        self.insert_rows()
        for table in ("participants", "collections"):
            self.assertEqual(
                parse_time(self.read_row(table)["updated_at"]), parse_time(CREATED),
            )

    def test_participant_email_migration_allows_multiple_ids_and_preserves_jobs(self):
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE participants (
                    participant_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    token_hash BLOB NOT NULL UNIQUE, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, revoked_at TEXT);
                CREATE TABLE collections (
                    job_id TEXT PRIMARY KEY,
                    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    state TEXT NOT NULL, tool TEXT NOT NULL, created_at TEXT NOT NULL,
                    consented_at TEXT, upload_confirmed_at TEXT, completed_at TEXT);
                """)
            conn.execute("INSERT INTO participants VALUES('participant-old','person@example.com',?,'revoked',?,?)",
                         (b"old-hash", CREATED, COMPLETED))
            conn.execute("INSERT INTO collections VALUES('old-job','participant-old','created','codex',?,NULL,NULL,NULL)", (CREATED,))

        self.db.initialize()
        with self.sqlite_connection() as db:
            db.execute(
                "INSERT INTO participants(participant_id,email,token_hash,status,created_at) VALUES(?,?,?,'active',?)",
                ("participant-new", "person@example.com", b"new-hash", CREATED),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO users(email,created_at) VALUES(?,?)", ("person@example.com", CREATED))
            rows = db.execute(
                "SELECT participant_id FROM participants WHERE email=? ORDER BY participant_id",
                ("person@example.com",),
            ).fetchall()
            self.assertEqual([row["participant_id"] for row in rows], ["participant-new", "participant-old"])
            old_job = db.execute("SELECT participant_id FROM collections WHERE job_id='old-job'").fetchone()
            self.assertEqual(old_job["participant_id"], "participant-old")
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

if __name__ == "__main__":
    unittest.main()
