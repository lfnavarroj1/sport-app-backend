"""HU036: proyecciones `session_monitoring_view` y `session_risk_alert_view` (ADR-044).

Expansión de la base de lectura sin backfill: los resúmenes y las alertas llegan por
`medicion.recibida.v1` y `riesgo.detectado.v1`, y el proyector se despliega antes o junto con
el productor. No se proyecta cada muestra. Índice de las alertas por sesión y instante. La
reversión elimina las tablas; la base de lectura es reconstruible.

Revision ID: 0011_session_monitoring_views
Revises: 0010_training_plan_template_view
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_session_monitoring_views"
down_revision: str | None = "0010_training_plan_template_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONITORING = "session_monitoring_view"
ALERTS = "session_risk_alert_view"
ALERT_ORDER_INDEX = "ix_session_risk_alert_view_session_order"


def upgrade() -> None:
    op.create_table(
        MONITORING,
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("last_measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_value", sa.SmallInteger(), nullable=False),
        sa.Column("samples_count", sa.BigInteger(), nullable=False),
        sa.Column("max_value", sa.SmallInteger(), nullable=False),
        sa.Column("avg_value", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("value_sum", sa.Numeric(precision=14, scale=1), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_session_monitoring_view")),
    )
    op.create_table(
        ALERTS,
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_session_risk_alert_view")),
    )
    op.create_index(
        ALERT_ORDER_INDEX, ALERTS, ["session_id", sa.text("detected_at DESC")], unique=False
    )


def downgrade() -> None:
    op.drop_index(ALERT_ORDER_INDEX, table_name=ALERTS)
    op.drop_table(ALERTS)
    op.drop_table(MONITORING)
