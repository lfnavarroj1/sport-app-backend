"""HU022 (ADR-039): índice de los inscritos de un evento en `event_registration_read`.

La consulta del organizador filtra por `event_id` y pagina por `registered_at`,
`registration_id` (keyset). HU041 solo creó el índice por deportista, así que esta revisión
agrega `(event_id, registered_at, registration_id)`. Solo expansión, sin cambio de datos; la
reversión elimina el índice.

Revision ID: 0006_registration_event_order
Revises: 0005_event_read_publication
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_registration_event_order"
down_revision: str | None = "0005_event_read_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_event_registration_read_event_order"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "event_registration_read",
        ["event_id", "registered_at", "registration_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="event_registration_read")
