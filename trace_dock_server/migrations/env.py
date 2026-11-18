import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from trace_dock_server.models import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url():
    load_dotenv(os.environ.get("TRACE_DOCK_ENV_FILE", ".env"))
    url = os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def run_migrations_offline():
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection):
    is_sqlite = connection.dialect.name == "sqlite"
    raw_connection = connection.connection.driver_connection if is_sqlite else None
    if raw_connection is not None:
        raw_connection.execute("PRAGMA foreign_keys=OFF")
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
            transactional_ddl=not is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if raw_connection is not None:
            raw_connection.execute("PRAGMA foreign_keys=ON")
            violations = raw_connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"database migration broke foreign keys: {violations!r}")


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _run_with_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
