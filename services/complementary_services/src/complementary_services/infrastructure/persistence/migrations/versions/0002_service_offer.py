"""HU015: servicio en borrador, disponibilidad por intervalos e idempotencia (ADR-027).

Solo expansión (tablas nuevas): compatible con la versión anterior del código.
La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0002_service_offer
Revises: 0001_baseline
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_service_offer"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("modality", sa.String(length=16), nullable=False),
        sa.Column("price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("billing_unit", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('SPORTS_MEDICINE', 'COACHING', 'NUTRITION', 'TRANSPORT', "
            "'ACCOMPANIMENT')",
            name=op.f("ck_service_category"),
        ),
        sa.CheckConstraint(
            "modality IN ('IN_PERSON', 'VIRTUAL', 'HYBRID')", name=op.f("ck_service_modality")
        ),
        sa.CheckConstraint("currency = 'COP'", name=op.f("ck_service_currency")),
        sa.CheckConstraint(
            "billing_unit IN ('SERVICE', 'SESSION', 'HOUR', 'DAY')",
            name=op.f("ck_service_billing_unit"),
        ),
        sa.CheckConstraint(
            "price_amount BETWEEN 0 AND 999999999.99", name=op.f("ck_service_price_amount")
        ),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name=op.f("ck_service_status")),
        sa.CheckConstraint("char_length(name) BETWEEN 3 AND 120", name=op.f("ck_service_name")),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 20 AND 2000", name=op.f("ck_service_description")
        ),
        sa.PrimaryKeyConstraint("service_id", name=op.f("pk_service")),
    )
    op.create_index(op.f("ix_service_provider_id"), "service", ["provider_id"], unique=False)
    op.create_index(op.f("ix_service_status"), "service", ["status"], unique=False)
    op.create_table(
        "service_availability",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_zone", sa.String(length=64), nullable=False),
        sa.Column("capacity", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_service_availability_interval")),
        sa.CheckConstraint(
            "ends_at - starts_at <= interval '24 hours'",
            name=op.f("ck_service_availability_duration"),
        ),
        sa.CheckConstraint(
            "capacity BETWEEN 1 AND 100", name=op.f("ck_service_availability_capacity")
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["service.service_id"],
            name=op.f("fk_service_availability_service_id_service"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("service_id", "position", name=op.f("pk_service_availability")),
    )
    op.create_table(
        "idempotency_key",
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_id", "key", name=op.f("pk_idempotency_key")),
    )
    op.create_index(
        op.f("ix_idempotency_key_expires_at"), "idempotency_key", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_key_expires_at"), table_name="idempotency_key")
    op.drop_table("idempotency_key")
    op.drop_table("service_availability")
    op.drop_index(op.f("ix_service_status"), table_name="service")
    op.drop_index(op.f("ix_service_provider_id"), table_name="service")
    op.drop_table("service")
