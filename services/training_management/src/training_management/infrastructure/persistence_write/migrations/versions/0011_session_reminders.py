"""HU038 (ADR-045 §5): preferencias de recordatorios durante la sesión.

Solo expansión, compatible con la versión anterior del código:
- `session_reminder_preference`: por deportista, habilitación e intervalo de `HYDRATION`
  (10-60 min) y `NUTRITION` (30-120 min), con `version` para el control optimista. Sin fila
  valen los valores por defecto del ruleset `session-reminders-2026-10-13.v1`, que vive en el
  código (no es una tabla administrable).

La reversión elimina la tabla; solo es segura antes de tener datos reales (pierde las
preferencias, y los deportistas vuelven a los valores por defecto).

Revision ID: 0011_session_reminders
Revises: 0010_monitoring
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_session_reminders"
down_revision: str | None = "0010_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_reminder_preference"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("hydration_enabled", sa.Boolean(), nullable=False),
        sa.Column("hydration_interval_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("nutrition_enabled", sa.Boolean(), nullable=False),
        sa.Column("nutrition_interval_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "hydration_interval_minutes BETWEEN 10 AND 60",
            name=op.f("ck_session_reminder_preference_hydration_interval_minutes"),
        ),
        sa.CheckConstraint(
            "nutrition_interval_minutes BETWEEN 30 AND 120",
            name=op.f("ck_session_reminder_preference_nutrition_interval_minutes"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_session_reminder_preference_version")),
        sa.PrimaryKeyConstraint("athlete_id", name=op.f("pk_session_reminder_preference")),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
