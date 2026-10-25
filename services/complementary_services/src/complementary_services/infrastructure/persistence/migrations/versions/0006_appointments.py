"""HU040: citas virtuales del deportista con un entrenador (ADR-046).

- `service_availability.availability_id`: identificador estable del intervalo
  (`gen_random_uuid()`, único). Al agregar la columna PostgreSQL calcula el valor por fila,
  así que los intervalos existentes quedan identificados sin un relleno aparte.
- `service_availability.booked_count`: citas activas del intervalo (0 por defecto), nunca
  más que su capacidad.
- `appointment` en la base única: claves foráneas locales al servicio y al intervalo,
  `athlete_id`/`provider_id` como identificadores de Usuarios, estado `BOOKED` /
  `CANCELLED_BY_ATHLETE`, `cancelled_at` solo en las canceladas, `meeting_url` reservado.
- Índice único parcial `(availability_id, athlete_id)` donde `status = 'BOOKED'`: una sola
  cita activa por deportista e intervalo; índices de «mis citas» del deportista y del
  profesional por `starts_at` y `appointment_id`.

Solo expansión: el código anterior inserta intervalos sin las columnas nuevas y reciben sus
valores por defecto. La reversión borra las citas y las columnas; solo es segura antes de
tener citas reales.

Revision ID: 0006_appointments
Revises: 0005_service_association
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_appointments"
down_revision: str | None = "0005_service_association"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_availability",
        sa.Column(
            "availability_id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.add_column(
        "service_availability",
        sa.Column("booked_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint(
        op.f("uq_service_availability_availability_id"),
        "service_availability",
        ["availability_id"],
    )
    op.create_check_constraint(
        op.f("ck_service_availability_booked_count"),
        "service_availability",
        "booked_count BETWEEN 0 AND capacity",
    )
    op.create_table(
        "appointment",
        sa.Column("appointment_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("availability_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_zone", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("meeting_url", sa.String(length=2048), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('BOOKED', 'CANCELLED_BY_ATHLETE')", name=op.f("ck_appointment_status")
        ),
        sa.CheckConstraint(
            "(status = 'BOOKED' AND cancelled_at IS NULL) "
            "OR (status = 'CANCELLED_BY_ATHLETE' AND cancelled_at IS NOT NULL)",
            name=op.f("ck_appointment_cancelled_at"),
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_appointment_interval")),
        sa.ForeignKeyConstraint(
            ["service_id"], ["service.service_id"], name=op.f("fk_appointment_service_id_service")
        ),
        sa.ForeignKeyConstraint(
            ["availability_id"],
            ["service_availability.availability_id"],
            name=op.f("fk_appointment_availability_id_service_availability"),
        ),
        sa.PrimaryKeyConstraint("appointment_id", name=op.f("pk_appointment")),
    )
    op.create_index(
        "uq_appointment_booked",
        "appointment",
        ["availability_id", "athlete_id"],
        unique=True,
        postgresql_where=sa.text("status = 'BOOKED'"),
    )
    op.create_index(
        "ix_appointment_athlete_id_starts_at",
        "appointment",
        ["athlete_id", "starts_at", "appointment_id"],
        unique=False,
    )
    op.create_index(
        "ix_appointment_provider_id_starts_at",
        "appointment",
        ["provider_id", "starts_at", "appointment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_appointment_provider_id_starts_at", table_name="appointment")
    op.drop_index("ix_appointment_athlete_id_starts_at", table_name="appointment")
    op.drop_index(
        "uq_appointment_booked",
        table_name="appointment",
        postgresql_where=sa.text("status = 'BOOKED'"),
    )
    op.drop_table("appointment")
    op.drop_constraint(
        op.f("ck_service_availability_booked_count"), "service_availability", type_="check"
    )
    op.drop_constraint(
        op.f("uq_service_availability_availability_id"), "service_availability", type_="unique"
    )
    op.drop_column("service_availability", "booked_count")
    op.drop_column("service_availability", "availability_id")
