"""Two-table persistence. SQLite is restricted to explicit local development."""
import contextlib
import sqlite3
import threading

COUNTERS = (
    "discovered_count", "selected_count", "workspace_exists_count",
    "reconstructable_count", "archive_only_count", "invalid_count", "excluded_count",
    "large_file_missing_remote_path_count", "uploaded_bytes", "verified_object_count",
)


class Database:
    def __init__(self, url: str, allow_sqlite: bool = False):
        self.url = url
        self.sqlite = url.startswith("sqlite:///")
        self.lock = threading.RLock()
        if self.sqlite and not allow_sqlite:
            raise RuntimeError("SQLite requires TRACE_DOCK_DEV=1")
        if not self.sqlite and not url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("PostgreSQL DATABASE_URL required")

    @contextlib.contextmanager
    def connect(self):
        with self.lock if self.sqlite else contextlib.nullcontext():
            if self.sqlite:
                conn = sqlite3.connect(self.url[len("sqlite:///"):], timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
            else:
                import psycopg
                from psycopg.rows import dict_row
                conn = psycopg.connect(self.url, row_factory=dict_row)
            try:
                yield Connection(conn, self.sqlite)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        binary = "BLOB" if self.sqlite else "BYTEA"
        timestamp = "TEXT" if self.sqlite else "TIMESTAMPTZ"
        updated_default = "''" if self.sqlite else "CURRENT_TIMESTAMP"
        with self.connect() as db:
            if not self.sqlite:
                # Serialize startup DDL when multiple API nodes initialize together.
                db.execute("SELECT pg_advisory_xact_lock(71349062510721)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                token_hash {binary} NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                created_at {timestamp} NOT NULL, revoked_at {timestamp},
                updated_at {timestamp} NOT NULL DEFAULT {updated_default})""")
            counts = ",".join(f"{k} BIGINT NOT NULL DEFAULT 0 CHECK({k}>=0)" for k in COUNTERS)
            db.execute(f"""CREATE TABLE IF NOT EXISTS collections (
                job_id TEXT PRIMARY KEY, participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                device_id TEXT NOT NULL, machine_name TEXT NOT NULL, client_operation_id TEXT NOT NULL,
                create_fingerprint TEXT NOT NULL, rules_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                ('created','collecting','awaiting_upload_confirmation','uploading','verifying','completed','resumable','failed','cancelled')),
                tool TEXT NOT NULL CHECK(tool IN ('codex','claude')),
                selection_mode TEXT CHECK(selection_mode IN ('all','partial')),
                selection_sha256 TEXT, {counts},
                manifest_key TEXT, manifest_crc64 TEXT, manifest_size_bytes BIGINT,
                created_at {timestamp} NOT NULL, consented_at {timestamp},
                upload_confirmed_at {timestamp}, completed_at {timestamp},
                updated_at {timestamp} NOT NULL DEFAULT {updated_default},
                UNIQUE(participant_id,client_operation_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS collections_participant_created ON collections(participant_id,created_at)")
            if self.sqlite:
                columns = list(db.execute("PRAGMA table_info(collections)"))
                if not any(row["name"] == "manifest_crc64" for row in columns):
                    db.execute("ALTER TABLE collections ADD COLUMN manifest_crc64 TEXT")
            else:
                db.execute("ALTER TABLE collections ADD COLUMN IF NOT EXISTS manifest_crc64 TEXT")
            self._initialize_updated_at(db)

    def _initialize_updated_at(self, db):
        # Historical modification times were not recorded. Use the latest known
        # lifecycle timestamp instead of claiming startup time was a user update.
        backfills = {
            "participants": "COALESCE(revoked_at, created_at)",
            "collections": "COALESCE(completed_at, upload_confirmed_at, consented_at, created_at)",
        }
        if not self.sqlite:
            db.execute("""CREATE OR REPLACE FUNCTION trace_dock_set_updated_at()
                RETURNS TRIGGER LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP = 'INSERT' THEN
                        NEW.updated_at := NEW.created_at;
                    ELSIF (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at') THEN
                        NEW.updated_at := clock_timestamp();
                    ELSE
                        NEW.updated_at := OLD.updated_at;
                    END IF;
                    RETURN NEW;
                END;
                $$""")
        for table, backfill in backfills.items():
            if self.sqlite:
                columns = list(db.execute(f"PRAGMA table_info({table})"))
                if not any(row["name"] == "updated_at" for row in columns):
                    # SQLite ADD COLUMN forbids a CURRENT_TIMESTAMP default.
                    # INSERT triggers fill this constant default before commit.
                    db.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                db.execute(f"UPDATE {table} SET updated_at={backfill} WHERE updated_at IS NULL OR updated_at=''")
                columns = list(db.execute(f"PRAGMA table_info({table})"))
                changed = " OR ".join(f'NEW."{row["name"]}" IS NOT OLD."{row["name"]}"'
                                      for row in columns if row["name"] != "updated_at")
                key = "participant_id" if table == "participants" else "job_id"
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_updated_at_insert
                    AFTER INSERT ON {table}
                    WHEN NEW.updated_at IS NOT NEW.created_at
                    BEGIN
                        UPDATE {table} SET updated_at=NEW.created_at WHERE {key}=NEW.{key};
                    END""")
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_updated_at_update
                    AFTER UPDATE ON {table} WHEN {changed}
                    BEGIN
                        UPDATE {table} SET updated_at=CASE
                            WHEN julianday('now') <= julianday(OLD.updated_at) THEN OLD.updated_at
                            ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now') END
                        WHERE {key}=NEW.{key};
                    END""")
            else:
                db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
                db.execute(f"UPDATE {table} SET updated_at={backfill} WHERE updated_at IS NULL")
                db.execute(f"ALTER TABLE {table} ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP")
                db.execute(f"ALTER TABLE {table} ALTER COLUMN updated_at SET NOT NULL")
                db.execute(f"DROP TRIGGER IF EXISTS {table}_updated_at ON {table}")
                db.execute(f"""CREATE TRIGGER {table}_updated_at
                    BEFORE INSERT OR UPDATE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION trace_dock_set_updated_at()""")


class Connection:
    def __init__(self, conn, sqlite):
        self.conn, self.sqlite = conn, sqlite

    def execute(self, query, params=()):
        if not self.sqlite:
            query = query.replace("?", "%s")
        return self.conn.execute(query, params)

    def one(self, query, params=(), lock=False):
        if lock and not self.sqlite:
            query += " FOR UPDATE"
        row = self.execute(query, params).fetchone()
        return dict(row) if row is not None else None
