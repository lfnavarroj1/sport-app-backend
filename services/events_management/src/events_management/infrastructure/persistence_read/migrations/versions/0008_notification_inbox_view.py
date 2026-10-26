"""HU039: bandeja de notificaciones `notification_inbox_view` (ADR-045 §10).

La proyecta `eventos-read-projector` desde `notificacion.solicitada.v1`; `read_at` lo fija
`POST /v1/me/notifications/{notification_id}/read`. Índice `(recipient_id, created_at,
notification_id)` para el keyset `created_at DESC, notification_id DESC`. Solo expansión;
la reversión elimina la vista (reproyectar la reconstruye sin las marcas de lectura).

Revision ID: 0008_notification_inbox_view
Revises: 0007_event_read_fee
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_notification_inbox_view"
down_revision: str | None = "0007_event_read_fee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_inbox_view",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("notification_id", name=op.f("pk_notification_inbox_view")),
    )
    op.create_index(
        "ix_notification_inbox_view_recipient_order",
        "notification_inbox_view",
        ["recipient_id", "created_at", "notification_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_inbox_view_recipient_order", table_name="notification_inbox_view"
    )
    op.drop_table("notification_inbox_view")
