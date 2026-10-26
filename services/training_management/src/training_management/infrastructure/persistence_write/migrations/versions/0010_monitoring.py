"""HU036 (ADR-044): monitoreo en tiempo real, umbral y alertas de riesgo.

Solo expansión, compatible con la versión anterior del código:
- `monitoring_settings`: umbral configurado por el deportista (sin fila → valor por defecto);
- `session_monitoring_state`: secuencias del monitoreo por sesión (versión del resumen de
  `medicion.recibida.v1` y número de alertas); su bloqueo serializa los lotes de la sesión;
- `training_risk_alert`: alertas `HR_ABOVE_THRESHOLD_V1` confirmadas;
- `training_session.end_reason` (nulo): `RISK_STOP` al detener la práctica;
- índice por `measured_at` en `training_measurement` para la retención de 90 días.

La reversión elimina las tablas, la columna y el índice; solo es segura antes de tener datos
reales (pierde umbrales, alertas y motivos).

Revision ID: 0010_monitoring
Revises: 0009_devices_measurements
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_monitoring"
down_revision: str | None = "0009_devices_measurements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTINGS = "monitoring_settings"
STATE = "session_monitoring_state"
ALERT = "training_risk_alert"
SESSION = "training_session"
MEASUREMENT = "training_measurement"
ALERT_INDEX = "ix_training_risk_alert_session_detected"
RETENTION_INDEX = "ix_training_measurement_measured_at"


def upgrade() -> None:
    op.create_table(
        SETTINGS,
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("max_heart_rate_alert_bpm", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_heart_rate_alert_bpm BETWEEN 100 AND 220",
            name=op.f("ck_monitoring_settings_max_heart_rate_alert_bpm"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_monitoring_settings_version")),
        sa.PrimaryKeyConstraint("athlete_id", name=op.f("pk_monitoring_settings")),
    )
    op.create_table(
        STATE,
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("summary_version", sa.BigInteger(), nullable=False),
        sa.Column("alert_count", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "summary_version >= 0", name=op.f("ck_session_monitoring_state_summary_version")
        ),
        sa.CheckConstraint(
            "alert_count >= 0", name=op.f("ck_session_monitoring_state_alert_count")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["training_session.session_id"],
            name=op.f("fk_session_monitoring_state_session_id_training_session"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_session_monitoring_state")),
    )
    op.create_table(
        ALERT,
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("threshold_bpm", sa.SmallInteger(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rule = 'HR_ABOVE_THRESHOLD_V1'", name=op.f("ck_training_risk_alert_rule")
        ),
        sa.CheckConstraint("severity = 'HIGH'", name=op.f("ck_training_risk_alert_severity")),
        sa.CheckConstraint(
            "threshold_bpm BETWEEN 100 AND 220", name=op.f("ck_training_risk_alert_threshold_bpm")
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_training_risk_alert_sequence")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["training_session.session_id"],
            name=op.f("fk_training_risk_alert_session_id_training_session"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_training_risk_alert")),
    )
    op.create_index(ALERT_INDEX, ALERT, ["session_id", "detected_at"], unique=False)
    op.add_column(SESSION, sa.Column("end_reason", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_training_session_end_reason"),
        SESSION,
        "end_reason IS NULL OR (end_reason = 'RISK_STOP' AND status = 'FINISHED')",
    )
    op.create_index(RETENTION_INDEX, MEASUREMENT, ["measured_at"], unique=False)


def downgrade() -> None:
    op.drop_index(RETENTION_INDEX, table_name=MEASUREMENT)
    op.drop_constraint(op.f("ck_training_session_end_reason"), SESSION, type_="check")
    op.drop_column(SESSION, "end_reason")
    op.drop_index(ALERT_INDEX, table_name=ALERT)
    op.drop_table(ALERT)
    op.drop_table(STATE)
    op.drop_table(SETTINGS)
