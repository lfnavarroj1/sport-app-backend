"""HU037 (ADR-044): dispositivos de medición y mediciones de frecuencia cardiaca.

Solo expansión (dos tablas nuevas): compatible con la versión anterior del código. La
vinculación reutiliza `command_idempotency` (operación `register_device`) y no usa la outbox.
`training_measurement` deduplica por su clave primaria (`session_id`, `device_id`,
`measured_at`). La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0009_devices_measurements
Revises: 0008_training_plan_templates
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_devices_measurements"
down_revision: str | None = "0008_training_plan_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEVICE = "athlete_device"
MEASUREMENT = "training_measurement"


def upgrade() -> None:
    op.create_table(
        DEVICE,
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=60), nullable=False),
        sa.Column("device_type", sa.String(length=32), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("device_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_athlete_device_device_id")),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 60",
            name=op.f("ck_athlete_device_display_name"),
        ),
        sa.CheckConstraint(
            "device_type = 'HEART_RATE_MONITOR'", name=op.f("ck_athlete_device_device_type")
        ),
        sa.CheckConstraint("protocol = 'BLE_HRS'", name=op.f("ck_athlete_device_protocol")),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_athlete_device_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_athlete_device_version")),
        sa.CheckConstraint(
            "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
            name=op.f("ck_athlete_device_revoked_at"),
        ),
        sa.PrimaryKeyConstraint("athlete_id", "device_id", name=op.f("pk_athlete_device")),
    )
    op.create_table(
        MEASUREMENT,
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False),
        sa.CheckConstraint("type = 'HEART_RATE'", name=op.f("ck_training_measurement_type")),
        sa.CheckConstraint("unit = 'bpm'", name=op.f("ck_training_measurement_unit")),
        sa.CheckConstraint(
            "value BETWEEN 20 AND 250", name=op.f("ck_training_measurement_value")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["training_session.session_id"],
            name=op.f("fk_training_measurement_session_id_training_session"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "session_id", "device_id", "measured_at", name=op.f("pk_training_measurement")
        ),
    )


def downgrade() -> None:
    op.drop_table(MEASUREMENT)
    op.drop_table(DEVICE)
