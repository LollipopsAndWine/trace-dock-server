from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trace_dock_server.db import Database


CREATED = "2000-01-01T00:00:00+00:00"
CONSENTED = "2000-01-02T00:00:00+00:00"
CONFIRMED = "2000-01-03T00:00:00+00:00"
COMPLETED = "2000-01-04T00:00:00+00:00"


class DatabaseTimestampTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.db"
        self.url = "sqlite:///" + str(self.path)
        self.db = Database(self.url, allow_sqlite=True)

    def insert_rows(self):
        with self.db.connect() as db:
            db.execute("""INSERT INTO participants(participant_id,email,token_hash,status,created_at)
                VALUES('participant','test@example.com',?,'active',?)""", (b"test-token-hash", CREATED))
            db.execute("""INSERT INTO collections(job_id,participant_id,device_id,machine_name,
                client_operation_id,create_fingerprint,rules_json,state,tool,created_at)
                VALUES('job','participant','device','machine','operation','fingerprint','{}','created','codex',?)""", (CREATED,))

    def read_row(self, table):
        with self.db.connect() as db:
            return db.one(f"SELECT * FROM {table}")

    def test_inserts_updates_noops_and_rollbacks(self):
        self.db.initialize()
        self.insert_rows()
        for table in ("participants", "collections"):
            with self.subTest(table=table):
                row = self.read_row(table)
                self.assertEqual(row["updated_at"], row["created_at"])
                with self.db.connect() as db:
                    if table == "participants":
                        db.execute("UPDATE participants SET status='revoked',revoked_at=?", (COMPLETED,))
                    else:
                        db.execute("UPDATE collections SET state='collecting',selected_count=1")
                changed = self.read_row(table)
                self.assertEqual(changed["created_at"], CREATED)
                modified = datetime.fromisoformat(changed["updated_at"].replace("Z", "+00:00"))
                self.assertGreater(modified, datetime.fromisoformat(CREATED))
                self.assertEqual(modified.utcoffset(), timezone.utc.utcoffset(None))
                with self.db.connect() as db:
                    db.execute(f"UPDATE {table} SET state=state" if table == "collections" else "UPDATE participants SET status=status")
                self.assertEqual(self.read_row(table)["updated_at"], changed["updated_at"])
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with self.db.connect() as db:
                        if table == "participants":
                            db.execute("UPDATE participants SET status='active',revoked_at=NULL")
                        else:
                            db.execute("UPDATE collections SET state='cancelled'")
                        raise RuntimeError("rollback")
                self.assertEqual(self.read_row(table), changed)
        with self.db.connect() as db:
            for table in ("participants", "collections"):
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
        with self.db.connect() as db:
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
        with self.db.connect() as db:
            self.assertEqual([dict(r) for r in db.execute("SELECT * FROM collections ORDER BY 1")], before_reinitialize)
            db.execute("UPDATE collections SET state='cancelled' WHERE job_id='new'")
            updated = db.one("SELECT * FROM collections WHERE job_id='new'")
            self.assertGreater(datetime.fromisoformat(updated["updated_at"].replace("Z", "+00:00")), datetime.fromisoformat(COMPLETED))
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

    def test_parallel_initialization_does_not_duplicate_migrations(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Database(self.url, allow_sqlite=True).initialize(), range(4)))
        self.insert_rows()
        for table in ("participants", "collections"):
            self.assertEqual(self.read_row(table)["updated_at"], CREATED)


if __name__ == "__main__":
    unittest.main()
