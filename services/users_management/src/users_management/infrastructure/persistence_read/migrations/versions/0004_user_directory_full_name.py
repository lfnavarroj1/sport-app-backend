"""HU011 (CA5, decisión del 2026-10-18): `full_name` en `user_directory`.

Expansión compatible: columna nulable. `usuarios-read-projector` la llena desde
`usuario.registrado.v1` para las cuentas nuevas; las filas proyectadas antes de esta
revisión las completa una sola vez `python -m users_management.workers.directory_backfill`
(reconstrucción controlada de CMP-001 desde la escritura). La reversión elimina la columna.

Revision ID: 0004_user_directory_full_name
Revises: 0003_athlete_profile_view
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_user_directory_full_name"
down_revision: str | None = "0003_athlete_profile_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_directory", sa.Column("full_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("user_directory", "full_name")
