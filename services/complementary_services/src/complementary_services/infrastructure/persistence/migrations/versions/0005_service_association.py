"""HU017: asociaciones del deportista a servicios médicos deportivos (ADR-038).

- `service_association` en la base única: `service_id` con clave foránea local al servicio,
  `athlete_id` como identificador de Usuarios, estado `ACTIVE`/`INACTIVE`, `ended_at` solo
  en las inactivas y versión.
- Índice único parcial `(service_id, athlete_id)` donde `status = 'ACTIVE'`: una sola
  asociación activa por deportista y servicio, también ante solicitudes concurrentes.
- Índice de «mis asociaciones» por deportista, `associated_at` y `association_id`.

Solo expansión (tabla e índices nuevos): compatible con la versión anterior del código. La
reversión borra la tabla y con ella el historial de asociaciones; solo es segura antes de
tener asociaciones reales.

Revision ID: 0005_service_association
Revises: 0004_service_availability_read
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_service_association"
down_revision: str | None = "0004_service_availability_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_association",
        sa.Column("association_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("associated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name=op.f("ck_service_association_status")
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND ended_at IS NULL) "
            "OR (status = 'INACTIVE' AND ended_at IS NOT NULL)",
            name=op.f("ck_service_association_ended_at"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["service.service_id"],
            name=op.f("fk_service_association_service_id_service"),
        ),
        sa.PrimaryKeyConstraint("association_id", name=op.f("pk_service_association")),
    )
    op.create_index(
        "uq_service_association_active",
        "service_association",
        ["service_id", "athlete_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_service_association_athlete_id_associated_at",
        "service_association",
        ["athlete_id", "associated_at", "association_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_association_athlete_id_associated_at", table_name="service_association"
    )
    op.drop_index(
        "uq_service_association_active",
        table_name="service_association",
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_table("service_association")
