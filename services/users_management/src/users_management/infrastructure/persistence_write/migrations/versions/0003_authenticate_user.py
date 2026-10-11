"""HU003: estado `disabled`, refresh tokens y ventana de intentos fallidos.

Expansión compatible con la versión anterior del código: solo agrega tablas y
amplía los valores permitidos de `user_account.status` (el código de HU001 sigue
escribiendo `active`). La reversión elimina las tablas y vuelve a restringir el
estado; falla a propósito si ya existen cuentas `disabled`.

Revision ID: 0003_authenticate_user
Revises: 0002_register_user
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_authenticate_user"
down_revision: str | None = "0002_register_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_user_account_status"), "user_account", type_="check")
    op.create_check_constraint(
        op.f("ck_user_account_status"), "user_account", "status IN ('active', 'disabled')"
    )
    op.create_table(
        "refresh_token",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_refresh_token_user_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("token_id", name=op.f("pk_refresh_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_token_token_hash")),
    )
    op.create_index(op.f("ix_refresh_token_family_id"), "refresh_token", ["family_id"])
    op.create_index(op.f("ix_refresh_token_user_id"), "refresh_token", ["user_id"])
    op.create_index(op.f("ix_refresh_token_expires_at"), "refresh_token", ["expires_at"])
    op.create_table(
        "login_failure_window",
        sa.Column("throttle_key", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("throttle_key", name=op.f("pk_login_failure_window")),
    )
    op.create_index(
        op.f("ix_login_failure_window_window_started_at"),
        "login_failure_window",
        ["window_started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_login_failure_window_window_started_at"), table_name="login_failure_window"
    )
    op.drop_table("login_failure_window")
    op.drop_index(op.f("ix_refresh_token_expires_at"), table_name="refresh_token")
    op.drop_index(op.f("ix_refresh_token_user_id"), table_name="refresh_token")
    op.drop_index(op.f("ix_refresh_token_family_id"), table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_constraint(op.f("ck_user_account_status"), "user_account", type_="check")
    op.create_check_constraint(op.f("ck_user_account_status"), "user_account", "status IN ('active')")
