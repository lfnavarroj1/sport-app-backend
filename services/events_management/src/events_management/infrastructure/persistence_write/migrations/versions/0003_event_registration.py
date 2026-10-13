"""HU041: inscripción a eventos publicados (ADR-032).

- `event_registration` con unicidad `(event_id, athlete_id)` y clave foránea local al evento.
- `command_idempotency` pasa a la clave (`actor_id`, `operation`, `key`): la columna
  `operation` ya existía (HU021), así que solo cambia la clave primaria.
- `event.status` admite `PUBLISHED` para que la inscripción pueda validarse en escritura;
  la transición de publicación pertenece a HU024.

La reversión elimina la tabla y restaura las restricciones; falla si ya existen eventos
`PUBLISHED` o claves repetidas por operación, y solo es segura antes de tener datos reales.

Revision ID: 0003_event_registration
Revises: 0002_event_draft
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_event_registration"
down_revision: str | None = "0002_event_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_event_status"), "event", type_="check")
    op.create_check_constraint(
        op.f("ck_event_status"), "event", "status IN ('DRAFT', 'PUBLISHED')"
    )
    op.drop_constraint(op.f("pk_command_idempotency"), "command_idempotency", type_="primary")
    op.create_primary_key(
        op.f("pk_command_idempotency"), "command_idempotency", ["actor_id", "operation", "key"]
    )
    op.create_table(
        "event_registration",
        sa.Column("registration_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_fee_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("applied_fee_currency", sa.String(length=3), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('REGISTERED')", name=op.f("ck_event_registration_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_event_registration_version")),
        sa.CheckConstraint(
            "(applied_fee_amount IS NULL) = (applied_fee_currency IS NULL)",
            name=op.f("ck_event_registration_applied_fee"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.event_id"],
            name=op.f("fk_event_registration_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("registration_id", name=op.f("pk_event_registration")),
        sa.UniqueConstraint(
            "event_id", "athlete_id", name="uq_event_registration_event_id_athlete_id"
        ),
    )
    op.create_index(
        op.f("ix_event_registration_event_id"), "event_registration", ["event_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_event_registration_event_id"), table_name="event_registration")
    op.drop_table("event_registration")
    op.drop_constraint(op.f("pk_command_idempotency"), "command_idempotency", type_="primary")
    op.create_primary_key(op.f("pk_command_idempotency"), "command_idempotency", ["actor_id", "key"])
    op.drop_constraint(op.f("ck_event_status"), "event", type_="check")
    op.create_check_constraint(op.f("ck_event_status"), "event", "status IN ('DRAFT')")
