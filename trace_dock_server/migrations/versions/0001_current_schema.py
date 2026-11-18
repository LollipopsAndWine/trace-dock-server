"""Bring pre-Alembic installations to the current modeled schema."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

revision = "0001_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def _tables(bind):
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table):
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table)}


def _add_timestamp(table, columns, sqlite):
    if "updated_at" not in columns:
        if sqlite:
            op.add_column(
                table,
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("''")),
            )
        else:
            op.add_column(table, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))


def _copy_users_from_participants(bind):
    if "participants" not in _tables(bind):
        return
    participants = sa.table(
        "participants",
        sa.column("email", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    users = sa.table(
        "users",
        sa.column("email", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    source = sa.select(participants.c.email, sa.func.min(participants.c.created_at)).group_by(participants.c.email)
    if bind.dialect.name == "sqlite":
        statement = sqlite_insert(users).from_select(["email", "created_at"], source).on_conflict_do_nothing(index_elements=["email"])
    else:
        statement = pg_insert(users).from_select(["email", "created_at"], source).on_conflict_do_nothing(index_elements=["email"])
    op.execute(statement)


def _backfill_updated_at(bind, table, columns):
    if "updated_at" not in columns or "created_at" not in columns:
        return
    candidates = [
        column for column in ("revoked_at", "completed_at", "upload_confirmed_at", "consented_at", "created_at")
        if column in columns
    ]
    relation = sa.table(
        table,
        *[sa.column(name, sa.DateTime(timezone=True)) for name in set(candidates + ["updated_at"])],
    )
    values = [relation.c[name] for name in candidates]
    value = sa.func.coalesce(*values) if len(values) > 1 else values[0]
    if table == "users":
        condition = sa.true()
    else:
        condition = relation.c.updated_at.is_(None)
    if table != "users" and bind.dialect.name == "sqlite":
        condition = sa.or_(condition, relation.c.updated_at == "")
    op.execute(relation.update().where(condition).values(updated_at=value))


def _normalize_sqlite_participants(bind):
    if "participants" not in _tables(bind):
        return
    inspector = sa.inspect(bind)
    unique_email = any(
        item.get("column_names") == ["email"]
        for item in inspector.get_unique_constraints("participants")
    )
    email_user_fk = any(
        item.get("referred_table") == "users"
        and item.get("constrained_columns") == ["email"]
        and item.get("referred_columns") == ["email"]
        for item in inspector.get_foreign_keys("participants")
    )
    if not unique_email and email_user_fk:
        return

    convention = {
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
    with op.batch_alter_table("participants", recreate="always", naming_convention=convention) as batch:
        if unique_email:
            batch.drop_constraint("uq_participants_email", type_="unique")
        if not email_user_fk:
            batch.create_foreign_key("participants_email_user_fk", "users", ["email"], ["email"])


def _normalize_postgres_participants(bind):
    if "participants" not in _tables(bind):
        return
    for constraint in sa.inspect(bind).get_unique_constraints("participants"):
        if constraint.get("column_names") == ["email"] and constraint.get("name"):
            op.drop_constraint(constraint["name"], "participants", type_="unique")
    foreign_keys = sa.inspect(bind).get_foreign_keys("participants")
    if not any(
        item.get("referred_table") == "users"
        and item.get("constrained_columns") == ["email"]
        and item.get("referred_columns") == ["email"]
        for item in foreign_keys
    ):
        op.create_foreign_key("participants_email_user_fk", "participants", "users", ["email"], ["email"])


def _create_initial_schema():
    op.create_table(
        "users",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "participants",
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("status IN ('active','revoked')", name="participants_status_check"),
        sa.ForeignKeyConstraint(["email"], ["users.email"], name="participants_email_user_fk"),
        sa.PrimaryKeyConstraint("participant_id"),
        sa.UniqueConstraint("token_hash", name="participants_token_hash_key"),
    )
    collection_columns = [
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("participant_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("machine_name", sa.Text(), nullable=False),
        sa.Column("client_operation_id", sa.Text(), nullable=False),
        sa.Column("create_fingerprint", sa.Text(), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("selection_mode", sa.Text(), nullable=True),
        sa.Column("selection_sha256", sa.Text(), nullable=True),
    ]
    collection_columns.extend(
        sa.Column(counter, sa.BigInteger(), server_default=sa.text("0"), nullable=False)
        for counter in (
            "discovered_count", "selected_count", "workspace_exists_count", "reconstructable_count",
            "archive_only_count", "invalid_count", "excluded_count",
            "large_file_missing_remote_path_count", "uploaded_bytes", "verified_object_count",
        )
    )
    collection_columns.extend([
        sa.Column("manifest_key", sa.Text(), nullable=True),
        sa.Column("manifest_crc64", sa.Text(), nullable=True),
        sa.Column("manifest_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upload_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    ])
    collection_constraints = [
        sa.CheckConstraint(
            "state IN ('created','collecting','awaiting_upload_confirmation','uploading','verifying','completed','resumable','failed','cancelled')",
            name="collections_state_check",
        ),
        sa.CheckConstraint("tool IN ('codex','claude')", name="collections_tool_check"),
        sa.CheckConstraint("selection_mode IS NULL OR selection_mode IN ('all','partial')", name="collections_selection_mode_check"),
    ]
    collection_constraints.extend(
        sa.CheckConstraint(f"{counter} >= 0", name=f"collections_{counter}_check")
        for counter in (
            "discovered_count", "selected_count", "workspace_exists_count", "reconstructable_count",
            "archive_only_count", "invalid_count", "excluded_count",
            "large_file_missing_remote_path_count", "uploaded_bytes", "verified_object_count",
        )
    )
    collection_constraints.extend([
        sa.ForeignKeyConstraint(["participant_id"], ["participants.participant_id"], name="collections_participant_fk"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("participant_id", "client_operation_id", name="collections_participant_operation_key"),
    ])
    op.create_table("collections", *collection_columns, *collection_constraints)
    op.create_index("collections_participant_created", "collections", ["participant_id", "created_at"])


def upgrade():
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    tables = _tables(bind)
    if not tables.intersection({"users", "participants", "collections"}):
        _create_initial_schema()
        return

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("email", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.PrimaryKeyConstraint("email"),
        )

    _add_timestamp("users", _columns(bind, "users"), sqlite)
    if "participants" in tables:
        _add_timestamp("participants", _columns(bind, "participants"), sqlite)
    if "collections" in tables:
        collection_columns = _columns(bind, "collections")
        if "manifest_crc64" not in collection_columns:
            op.add_column("collections", sa.Column("manifest_crc64", sa.Text(), nullable=True))
        _add_timestamp("collections", collection_columns, sqlite)

    _copy_users_from_participants(bind)

    for table in ("users", "participants", "collections"):
        if table in _tables(bind):
            _backfill_updated_at(bind, table, _columns(bind, table))

    if "collections" in _tables(bind):
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("collections")}
        if "collections_participant_created" not in indexes:
            op.create_index("collections_participant_created", "collections", ["participant_id", "created_at"])

    if sqlite:
        _normalize_sqlite_participants(bind)
    else:
        _normalize_postgres_participants(bind)

    if not sqlite:
        for table in ("users", "participants", "collections"):
            if table in _tables(bind) and "updated_at" in _columns(bind, table):
                op.alter_column(
                    table,
                    "updated_at",
                    existing_type=sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.current_timestamp(),
                )


def downgrade():
    raise RuntimeError("This data migration is irreversible; restore a database backup instead.")
