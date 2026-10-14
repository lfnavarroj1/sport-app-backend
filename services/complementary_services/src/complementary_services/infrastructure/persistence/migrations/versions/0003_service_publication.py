"""HU019: publicación de servicios, outbox y consulta de servicios propios (ADR-027, ADR-038).

- `service.published_at` (nulo en borrador) y la restricción estado ↔ `published_at`; los
  servicios existentes son borradores de HU015, así que la cumplen sin backfill.
- Índice de «mis servicios» por profesional, `updated_at` y `service_id`.
- `outbox_event` con las columnas de la outbox de Eventos, el índice de pendientes y un
  índice parcial de publicados para la purga de siete días.

Solo expansión (columna nula, índices y tabla nuevos): compatible con la versión anterior
del código. La reversión borra la outbox y el instante de publicación; solo es segura
antes de tener publicaciones reales.

Revision ID: 0003_service_publication
Revises: 0002_service_offer
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_service_publication"
down_revision: str | None = "0002_service_offer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("service", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_service_published_at"),
        "service",
        "(status = 'DRAFT' AND published_at IS NULL) "
        "OR (status = 'PUBLISHED' AND published_at IS NOT NULL)",
    )
    op.create_index(
        "ix_service_provider_id_updated_at",
        "service",
        ["provider_id", "updated_at", "service_id"],
        unique=False,
    )
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("causation_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "idx_outbox_pending",
        "outbox_event",
        ["next_attempt_at", "occurred_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        "idx_outbox_published",
        "outbox_event",
        ["published_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_outbox_published",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NOT NULL"),
    )
    op.drop_index(
        "idx_outbox_pending",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_event")
    op.drop_index("ix_service_provider_id_updated_at", table_name="service")
    op.drop_constraint(op.f("ck_service_published_at"), "service", type_="check")
    op.drop_column("service", "published_at")
