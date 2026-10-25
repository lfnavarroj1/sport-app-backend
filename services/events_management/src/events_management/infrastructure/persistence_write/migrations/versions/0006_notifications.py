"""HU039: notificaciones de eventos masivos (ADR-045 §9-10).

- `notification` con `dedupe_key` único (`type:event_id:recipient_id`) y clave foránea
  local al evento.
- `notification_preference` por deportista (`event_matches`, `event_reminders`,
  `push_enabled`, `version`); sin fila rigen los valores por defecto.
- `push_token` (solo FCM): token cifrado (`token_ciphertext`) y `token_hash` único.
- `notification_delivery`: resultado del envío *push* por notificación y token.
- `outbox_event.ordering_key` opcional: `notificacion.solicitada.v1` se ordena por
  `recipient_id`; nulo equivale al `aggregate_id` de los eventos anteriores.

Solo expansión, compatible con la versión anterior del servicio. La reversión elimina las
tablas (se pierden notificaciones, preferencias y tokens) y la columna de la outbox.

Revision ID: 0006_notifications
Revises: 0005_event_fee
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_notifications"
down_revision: str | None = "0005_event_fee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_event", sa.Column("ordering_key", sa.String(length=128)))
    op.create_table(
        "notification",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "type IN ('EVENT_PUBLISHED_MATCH', 'EVENT_REMINDER_24H')",
            name=op.f("ck_notification_type"),
        ),
        sa.CheckConstraint(
            "priority IN ('NORMAL', 'HIGH')", name=op.f("ck_notification_priority")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120", name=op.f("ck_notification_title")
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 500", name=op.f("ck_notification_body")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.event_id"],
            name=op.f("fk_notification_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("notification_id", name=op.f("pk_notification")),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_dedupe_key"),
    )
    op.create_index(
        op.f("ix_notification_event_id"), "notification", ["event_id"], unique=False
    )
    op.create_table(
        "notification_preference",
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("event_matches", sa.Boolean(), nullable=False),
        sa.Column("event_reminders", sa.Boolean(), nullable=False),
        sa.Column("push_enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_notification_preference_version")),
        sa.PrimaryKeyConstraint("recipient_id", name=op.f("pk_notification_preference")),
    )
    op.create_table(
        "push_token",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("platform IN ('android')", name=op.f("ck_push_token_platform")),
        sa.PrimaryKeyConstraint("token_id", name=op.f("pk_push_token")),
        sa.UniqueConstraint("token_hash", name="uq_push_token_token_hash"),
    )
    op.create_index(
        op.f("ix_push_token_recipient_id"), "push_token", ["recipient_id"], unique=False
    )
    op.create_table(
        "notification_delivery",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('FCM')", name=op.f("ck_notification_delivery_channel")),
        sa.CheckConstraint(
            "status IN ('SENT', 'FAILED')", name=op.f("ck_notification_delivery_status")
        ),
        sa.CheckConstraint("attempts >= 1", name=op.f("ck_notification_delivery_attempts")),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.notification_id"],
            name=op.f("fk_notification_delivery_notification_id_notification"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["push_token.token_id"],
            name=op.f("fk_notification_delivery_token_id_push_token"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "notification_id",
            "token_id",
            name="uq_notification_delivery_notification_id_token_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_delivery")
    op.drop_index(op.f("ix_push_token_recipient_id"), table_name="push_token")
    op.drop_table("push_token")
    op.drop_table("notification_preference")
    op.drop_index(op.f("ix_notification_event_id"), table_name="notification")
    op.drop_table("notification")
    op.drop_column("outbox_event", "ordering_key")
