"""HU042: reportes de molestia, incapacidad o lesión (ADR-037).

Solo expansión (tabla nueva): la idempotencia reutiliza `command_idempotency` de HU032 con la
operación `report_health` (30 días) y la outbox existente. Catálogos cerrados como
restricciones `CHECK`, sin texto libre; `session_id` opcional referencia `training_session`
(misma base). La reversión elimina la tabla y solo es segura antes de tener datos reales.

Revision ID: 0013_health_reports
Revises: 0011_session_reminders
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_health_reports"
down_revision: str | None = "0011_session_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "health_report"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("body_area", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_recovery_days", sa.SmallInteger(), nullable=True),
        sa.Column("health_consent_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "type IN ('DISCOMFORT', 'INJURY', 'INCAPACITY')", name=op.f("ck_health_report_type")
        ),
        sa.CheckConstraint(
            "body_area IN ('HEAD', 'NECK', 'SHOULDER', 'ARM', 'ELBOW', 'WRIST_HAND', 'CHEST',"
            " 'BACK', 'HIP', 'THIGH', 'KNEE', 'LOWER_LEG', 'ANKLE_FOOT', 'OTHER')",
            name=op.f("ck_health_report_body_area"),
        ),
        sa.CheckConstraint(
            "severity IN ('LOW', 'MODERATE', 'HIGH')", name=op.f("ck_health_report_severity")
        ),
        sa.CheckConstraint(
            "expected_recovery_days IS NULL OR expected_recovery_days BETWEEN 0 AND 365",
            name=op.f("ck_health_report_expected_recovery_days"),
        ),
        sa.CheckConstraint("status IN ('OPEN')", name=op.f("ck_health_report_status")),
        sa.CheckConstraint("version = 1", name=op.f("ck_health_report_version")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["training_session.session_id"],
            name=op.f("fk_health_report_session_id_training_session"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_health_report")),
    )
    op.create_index(op.f("ix_health_report_athlete_id"), TABLE, ["athlete_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_health_report_athlete_id"), table_name=TABLE)
    op.drop_table(TABLE)
