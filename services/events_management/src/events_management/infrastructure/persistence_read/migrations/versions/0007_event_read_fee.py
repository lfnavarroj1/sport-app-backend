"""HU025 (ADR-040): tarifa del evento en `event_read`.

`fee_amount NUMERIC(12,2)`, `fee_currency` y `fee_updated_at`, opcionales. Las proyecta
`eventos-read-projector` por estado completo y versión mayor desde
`evento.tarifa_actualizada.v1` (y `evento.publicado.v1`, que también la transporta). Sin
contador de inscripciones (CMP-004). Solo expansión: las vistas existentes quedan sin tarifa
(ningún evento la tenía); la reversión elimina las columnas y se reconstruyen reproyectando.

Revision ID: 0007_event_read_fee
Revises: 0006_registration_event_order
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_event_read_fee"
down_revision: str | None = "0006_registration_event_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("event_read", sa.Column("fee_amount", sa.Numeric(precision=12, scale=2)))
    op.add_column("event_read", sa.Column("fee_currency", sa.String(length=3)))
    op.add_column("event_read", sa.Column("fee_updated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("event_read", "fee_updated_at")
    op.drop_column("event_read", "fee_currency")
    op.drop_column("event_read", "fee_amount")
