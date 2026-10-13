"""HU021: vista `event_read` y deduplicación `processed_event` (CMP-004).

Índice único por `event_id` (clave primaria) e índice por
(`organizer_id`, `status`, `starts_at`) para las consultas del organizador.
Solo expansión; la base de lectura es reconstruible desde los eventos.

Revision ID: 0002_event_read
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_event_read"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_read",
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
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_read")),
    )
    op.create_index(
        "ix_event_read_organizer_status_starts",
        "event_read",
        ["organizer_id", "status", "starts_at"],
        unique=False,
    )
    op.create_table(
        "processed_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_processed_event")),
    )


def downgrade() -> None:
    op.drop_table("processed_event")
    op.drop_index("ix_event_read_organizer_status_starts", table_name="event_read")
    op.drop_table("event_read")
