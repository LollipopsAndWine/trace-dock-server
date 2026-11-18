"""SQLAlchemy database setup. SQLite is restricted to explicit local development."""
import contextlib
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

_SQLITE_SCHEMA_LOCKS = {}
_SQLITE_SCHEMA_LOCKS_GUARD = threading.Lock()


class Database:
    def __init__(self, url: str, allow_sqlite: bool = False):
        self.url = url
        self.sqlite = url.startswith("sqlite:///")
        if self.sqlite and not allow_sqlite:
            raise RuntimeError("SQLite requires TRACE_DOCK_DEV=1")
        if not self.sqlite and not url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("PostgreSQL DATABASE_URL required")
        sqlalchemy_url = url
        if url.startswith("postgres://"):
            sqlalchemy_url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            sqlalchemy_url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.engine = create_engine(sqlalchemy_url, connect_args={"timeout": 30} if self.sqlite else {})
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")

            @event.listens_for(self.engine, "begin")
            def begin_immediate(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextlib.contextmanager
    def session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def initialize(self):
        if self.sqlite:
            with _SQLITE_SCHEMA_LOCKS_GUARD:
                lock = _SQLITE_SCHEMA_LOCKS.setdefault(self.url, threading.RLock())
            with lock:
                self._create_schema_and_upgrade()
            return

        self._create_schema_and_upgrade()

    def _create_schema_and_upgrade(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        with self.engine.connect() as connection:
            if not self.sqlite:
                connection.execute(select(func.pg_advisory_lock(71349062510721)))
                connection.commit()
            try:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                if connection.in_transaction():
                    connection.commit()
            finally:
                if not self.sqlite:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.execute(select(func.pg_advisory_unlock(71349062510721)))
                    connection.commit()
