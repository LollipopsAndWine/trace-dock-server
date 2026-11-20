"""Remove legacy triggers that truncate ORM-managed updated_at values."""
from alembic import op
import sqlalchemy as sa

revision = "0002_drop_legacy_timestamp_triggers"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("users", "participants", "collections"):
        if table not in tables:
            continue
        trigger = f"{table}_updated_at_update"
        if bind.dialect.name == "sqlite":
            op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger}"'))
        else:
            op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{table}"'))


def downgrade():
    # Trigger bodies varied by installation; restoring them would reintroduce
    # database-managed values that conflict with the ORM timestamp contract.
    pass
