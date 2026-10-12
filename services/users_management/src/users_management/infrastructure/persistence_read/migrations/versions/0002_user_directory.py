"""HU010: directorio mínimo de identidad en la base de lectura.

Tabla reconstruible que mantendrá `usuarios-read-projector` (refactor pendiente
de HU001) a partir de `usuario.registrado.v1`; ese refactor agregará su propia
`processed_event`. Solo expansión: compatible con la versión anterior.
La reversión elimina las tablas; se reconstruyen reproyectando los eventos.

Revision ID: 0002_user_directory
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_directory"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_directory",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('athlete', 'organizer', 'service_provider')",
            name=op.f("ck_user_directory_actor_type"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_user_directory_status")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_directory")),
        sa.UniqueConstraint("email", name=op.f("uq_user_directory_email")),
    )


def downgrade() -> None:
    op.drop_table("user_directory")
