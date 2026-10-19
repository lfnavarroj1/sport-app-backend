"""HU024: `event_read.published_at` (ADR-033).

ADR-033 la sugiere como `0004_event_read_publication`; el 0004 de esta historia de lectura
ya lo usa HU041 (`0004_event_registration_read`), así que va como 0005. El índice
`(status, starts_at)` ya existe desde `0003_event_read_calendar_index` (HU035). Solo
expansión; la base de lectura es reconstruible desde los eventos.

Revision ID: 0005_event_read_publication
Revises: 0004_event_registration_read
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_event_read_publication"
down_revision: str | None = "0004_event_registration_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_read", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("event_read", "published_at")
