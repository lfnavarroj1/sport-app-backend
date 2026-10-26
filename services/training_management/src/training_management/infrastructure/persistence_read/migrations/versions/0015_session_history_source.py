"""HU031: origen, referencia externa y distancia en `training_session_history` (ADR-042 §12).

Solo expansión, compatible con la versión anterior: `source` es `NOT NULL` con valor por
defecto `'SPORTAPP'` (las filas existentes son sesiones de HU032; en PostgreSQL ≥ 11 el
`ADD COLUMN ... DEFAULT` no reescribe la tabla), `external_ref` y `distance_meters` admiten
nulos (solo las actividades de Strava los tienen). La reversión elimina las columnas; la
base de lectura es reconstruible reproyectando `sportapp.entrenamientos.v1` y
`sportapp.terceros.v1`.

Revision ID: 0015_session_history_source
Revises: 0014_routine_template_view
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_session_history_source"
down_revision: str | None = "0014_routine_template_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "training_session_history"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'SPORTAPP'"),
        ),
    )
    op.add_column(TABLE, sa.Column("external_ref", sa.Uuid(), nullable=True))
    op.add_column(TABLE, sa.Column("distance_meters", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "distance_meters")
    op.drop_column(TABLE, "external_ref")
    op.drop_column(TABLE, "source")
