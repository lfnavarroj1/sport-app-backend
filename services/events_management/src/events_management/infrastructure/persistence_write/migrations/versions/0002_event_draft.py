"""HU021: evento en borrador, idempotencia de comandos y outbox (ADR-028, CMP-004).

Solo expansión (tablas nuevas): compatible con la versión anterior del código.
La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0002_event_draft
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_event_draft"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("discipline", sa.String(length=80), nullable=False),
        sa.Column("location_name", sa.String(length=120), nullable=False),
        sa.Column("location_address", sa.String(length=200), nullable=False),
        sa.Column("location_city", sa.String(length=80), nullable=False),
        sa.Column("location_country_code", sa.String(length=2), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_zone", sa.String(length=64), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT')", name=op.f("ck_event_status")),
        sa.CheckConstraint("char_length(name) BETWEEN 3 AND 120", name=op.f("ck_event_name")),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 20 AND 2000", name=op.f("ck_event_description")
        ),
        sa.CheckConstraint(
            "char_length(discipline) BETWEEN 2 AND 80", name=op.f("ck_event_discipline")
        ),
        sa.CheckConstraint(
            "char_length(location_name) BETWEEN 3 AND 120", name=op.f("ck_event_location_name")
        ),
        sa.CheckConstraint(
            "char_length(location_address) BETWEEN 5 AND 200",
            name=op.f("ck_event_location_address"),
        ),
        sa.CheckConstraint(
            "char_length(location_city) BETWEEN 2 AND 80", name=op.f("ck_event_location_city")
        ),
        sa.CheckConstraint(
            "location_country_code ~ '^[A-Z]{2}$'", name=op.f("ck_event_location_country_code")
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 15 AND 1440", name=op.f("ck_event_duration_minutes")
        ),
        sa.CheckConstraint("capacity BETWEEN 1 AND 100000", name=op.f("ck_event_capacity")),
        sa.CheckConstraint(
            "ends_at = starts_at + make_interval(mins => duration_minutes)",
            name=op.f("ck_event_ends_at"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_event_version")),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event")),
    )
    op.create_index(op.f("ix_event_organizer_id"), "event", ["organizer_id"], unique=False)
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("causation_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "idx_outbox_pending",
        "outbox_event",
        ["next_attempt_at", "occurred_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "command_idempotency",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("actor_id", "key", name=op.f("pk_command_idempotency")),
    )
    op.create_index(
        op.f("ix_command_idempotency_expires_at"),
        "command_idempotency",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_command_idempotency_expires_at"), table_name="command_idempotency")
    op.drop_table("command_idempotency")
    op.drop_index(
        "idx_outbox_pending",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_event")
    op.drop_index(op.f("ix_event_organizer_id"), table_name="event")
    op.drop_table("event")
