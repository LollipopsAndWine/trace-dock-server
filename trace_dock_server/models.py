"""Canonical SQLAlchemy schema for Trace Dock persistence."""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

COUNTERS = (
    "discovered_count", "selected_count", "workspace_exists_count",
    "reconstructable_count", "archive_only_count", "invalid_count", "excluded_count",
    "large_file_missing_remote_path_count", "uploaded_bytes", "verified_object_count",
)


def _created_timestamp(context):
    return context.get_current_parameters()["created_at"]


def _modified_timestamp():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        elif value is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[str] = mapped_column(
        UTCDateTime(), nullable=False, default=_created_timestamp,
        server_default=func.current_timestamp(), onupdate=_modified_timestamp,
    )


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        CheckConstraint("status IN ('active','revoked')", name="participants_status_check"),
        UniqueConstraint("token_hash", name="participants_token_hash_key"),
    )

    participant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, ForeignKey("users.email", name="participants_email_user_fk"), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[str] = mapped_column(
        UTCDateTime(), nullable=False, default=_created_timestamp,
        server_default=func.current_timestamp(), onupdate=_modified_timestamp,
    )


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint(
            "state IN ('created','collecting','awaiting_upload_confirmation','uploading','verifying','completed','resumable','failed','cancelled')",
            name="collections_state_check",
        ),
        CheckConstraint("tool IN ('codex','claude')", name="collections_tool_check"),
        CheckConstraint("selection_mode IS NULL OR selection_mode IN ('all','partial')", name="collections_selection_mode_check"),
        UniqueConstraint("participant_id", "client_operation_id", name="collections_participant_operation_key"),
        Index("collections_participant_created", "participant_id", "created_at"),
    ) + tuple(CheckConstraint(f"{counter} >= 0", name=f"collections_{counter}_check") for counter in COUNTERS)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("participants.participant_id", name="collections_participant_fk"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    machine_name: Mapped[str] = mapped_column(Text, nullable=False)
    client_operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    create_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    rules_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    selection_mode: Mapped[str | None] = mapped_column(Text)
    selection_sha256: Mapped[str | None] = mapped_column(Text)
    discovered_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    selected_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    workspace_exists_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    reconstructable_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    archive_only_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    invalid_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    excluded_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    large_file_missing_remote_path_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    verified_object_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    manifest_key: Mapped[str | None] = mapped_column(Text)
    manifest_crc64: Mapped[str | None] = mapped_column(Text)
    manifest_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[str] = mapped_column(UTCDateTime(), nullable=False)
    consented_at: Mapped[str | None] = mapped_column(UTCDateTime())
    upload_confirmed_at: Mapped[str | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[str | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[str] = mapped_column(
        UTCDateTime(), nullable=False, default=_created_timestamp,
        server_default=func.current_timestamp(), onupdate=_modified_timestamp,
    )
