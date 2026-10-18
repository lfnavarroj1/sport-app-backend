"""HU024: publicación de eventos masivos (ADR-033).

- `event.published_at` (nulo en borrador) y la restricción
  `(status = 'PUBLISHED') = (published_at IS NOT NULL)`. `ck_event_status` ya admite
  `PUBLISHED` desde `0003_event_registration` (HU041).
- Antes de la restricción, un evento `PUBLISHED` sin fecha (solo datos sembrados antes de
  HU024) recibe `published_at = updated_at` (decisión del responsable, 2026-10-18).
- `command_idempotency` se reutiliza con `operation = 'publish_event'`: sin cambios.

La reversión elimina la restricción y la columna; se pierden las fechas de publicación.

Revision ID: 0004_event_publication
Revises: 0003_event_registration
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_event_publication"
down_revision: str | None = "0003_event_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("event", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE event SET published_at = updated_at "
        "WHERE status = 'PUBLISHED' AND published_at IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_event_published_at"),
        "event",
        "(status = 'PUBLISHED') = (published_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_event_published_at"), "event", type_="check")
    op.drop_column("event", "published_at")
