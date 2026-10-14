"""HU020: índices de lectura del catálogo de disponibilidad (ADR-038).

- `service (status, category)`: servicios publicados filtrados por categoría.
- `service_availability (service_id, starts_at)`: intervalos de cada servicio que empiezan
  en el rango consultado.

Solo crea índices: compatible con la versión anterior del código y reversible sin pérdida
de datos.

Revision ID: 0004_service_availability_read
Revises: 0003_service_publication
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_service_availability_read"
down_revision: str | None = "0003_service_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_service_status_category", "service", ["status", "category"], unique=False)
    op.create_index(
        "ix_service_availability_service_id_starts_at",
        "service_availability",
        ["service_id", "starts_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_service_availability_service_id_starts_at", table_name="service_availability")
    op.drop_index("ix_service_status_category", table_name="service")
