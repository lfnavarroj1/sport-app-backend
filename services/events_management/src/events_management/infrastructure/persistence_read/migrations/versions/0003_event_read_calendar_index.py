"""HU035: índice `(status, starts_at)` de `event_read` para el calendario (ADR-031).

Solo agrega un índice a la base de lectura; reversión segura.

Revision ID: 0003_event_read_calendar_index
Revises: 0002_event_read
Create Date: 2026-10-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_event_read_calendar_index"
down_revision: str | None = "0002_event_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_event_read_status_starts", "event_read", ["status", "starts_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_event_read_status_starts", table_name="event_read")
