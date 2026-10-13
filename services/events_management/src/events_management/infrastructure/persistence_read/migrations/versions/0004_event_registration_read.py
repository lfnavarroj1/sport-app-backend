"""HU041: vista `event_registration_read` (ADR-032).

ADR-032 la nombra `0003_event_registration_read`; el número 0003 de esta historia de
lectura ya lo usa HU035 (`0003_event_read_calendar_index`), así que va como 0004.
Unicidad `(event_id, athlete_id)` e índice `(athlete_id, registered_at)`. Solo expansión;
la base de lectura es reconstruible desde los eventos.

Revision ID: 0004_event_registration_read
Revises: 0003_event_read_calendar_index
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_event_registration_read"
down_revision: str | None = "0003_event_read_calendar_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_registration_read",
        sa.Column("registration_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_fee_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("applied_fee_currency", sa.String(length=3), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("registration_id", name=op.f("pk_event_registration_read")),
        sa.UniqueConstraint(
            "event_id", "athlete_id", name="uq_event_registration_read_event_id_athlete_id"
        ),
    )
    op.create_index(
        "ix_event_registration_read_athlete_registered",
        "event_registration_read",
        ["athlete_id", "registered_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_registration_read_athlete_registered", table_name="event_registration_read"
    )
    op.drop_table("event_registration_read")
