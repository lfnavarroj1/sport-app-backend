"""HU016 (ADR-036): ciclo de cambio de la suscripción del deportista.

No toca `0003_plan_price_offers` (catálogo `plans-2026-10-13.v1` y siete ofertas):

- `subscription` agrega `periodicity`, `plan_offer_id`, `period_start` y `period_end`;
  `status` admite `ENDED` y `origin` admite `USER_CHANGE`;
- tablas `billing_commitment` (compromiso de pago informativo, `PENDING_EXTERNAL`/`WAIVED`),
  `subscription_history` (movimientos del estado de cuenta) y `command_idempotency`.

Relleno idempotente de las suscripciones existentes (todas son altas `FREE`):
`periodicity = NONE`, `period_start = started_at`, la oferta `NONE` de su versión si existe
(`FREE` v1 no tiene ofertas) y un movimiento `FREE_ACTIVATED` con identificador determinista,
así que repetir el relleno no duplica nada. Las columnas nuevas son nulas para que el código
anterior siga insertando durante el despliegue progresivo.

La reversión solo es posible mientras no existan suscripciones `ENDED` ni de origen
`USER_CHANGE` (borra compromisos e historial).

Revision ID: 0004_subscription_changes
Revises: 0003_plan_price_offers
Create Date: 2026-10-23
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_subscription_changes"
down_revision: str | None = "0003_plan_price_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Identificadores deterministas de los movimientos `FREE_ACTIVATED` (mismo cálculo que el
# consumidor del alta gratuita).
MOVEMENT_NAMESPACE = uuid.UUID("a5d0c9e2-7f41-5b36-8e2d-4c1b9f6a3e70")


def free_activated_movement_id(subscription_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(MOVEMENT_NAMESPACE, f"FREE_ACTIVATED:{subscription_id}")


def upgrade() -> None:
    op.add_column("subscription", sa.Column("periodicity", sa.String(length=16), nullable=True))
    op.add_column("subscription", sa.Column("plan_offer_id", sa.Uuid(), nullable=True))
    op.add_column(
        "subscription", sa.Column("period_start", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscription", sa.Column("period_end", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_subscription_plan_offer_id_plan_offer"),
        "subscription",
        "plan_offer",
        ["plan_offer_id"],
        ["offer_id"],
    )
    op.drop_constraint(op.f("ck_subscription_status"), "subscription", type_="check")
    op.create_check_constraint(
        op.f("ck_subscription_status"), "subscription", "status IN ('ACTIVE', 'ENDED')"
    )
    op.drop_constraint(op.f("ck_subscription_origin"), "subscription", type_="check")
    op.create_check_constraint(
        op.f("ck_subscription_origin"),
        "subscription",
        "origin IN ('AUTO_REGISTRATION', 'USER_CHANGE')",
    )
    op.create_check_constraint(
        op.f("ck_subscription_periodicity"),
        "subscription",
        "periodicity IS NULL OR periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')",
    )
    op.create_check_constraint(
        op.f("ck_subscription_ended"), "subscription", "(status = 'ENDED') = (ended_at IS NOT NULL)"
    )

    op.create_table(
        "billing_commitment",
        sa.Column("commitment_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("plan_offer_id", sa.Uuid(), nullable=False),
        sa.Column("periodicity", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING_EXTERNAL', 'WAIVED')", name=op.f("ck_billing_commitment_status")
        ),
        sa.CheckConstraint(
            "periodicity IN ('MONTHLY', 'QUARTERLY', 'ANNUAL')",
            name=op.f("ck_billing_commitment_periodicity"),
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_billing_commitment_amount")),
        sa.CheckConstraint("currency = 'COP'", name=op.f("ck_billing_commitment_currency")),
        sa.CheckConstraint(
            "period_end > period_start", name=op.f("ck_billing_commitment_period")
        ),
        sa.CheckConstraint(
            "(status = 'WAIVED') = (waived_at IS NOT NULL)",
            name=op.f("ck_billing_commitment_waived"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscription.subscription_id"],
            name=op.f("fk_billing_commitment_subscription_id_subscription"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_offer_id"],
            ["plan_offer.offer_id"],
            name=op.f("fk_billing_commitment_plan_offer_id_plan_offer"),
        ),
        sa.PrimaryKeyConstraint("commitment_id", name=op.f("pk_billing_commitment")),
    )
    op.create_index(
        "ix_billing_commitment_user_status", "billing_commitment", ["user_id", "status"]
    )

    op.create_table(
        "subscription_history",
        sa.Column("movement_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("movement_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("commitment_id", sa.Uuid(), nullable=True),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("periodicity", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "movement_type IN ('FREE_ACTIVATED', 'PLAN_CHANGED', 'RETURNED_TO_FREE', "
            "'COMMITMENT_CREATED', 'COMMITMENT_WAIVED')",
            name=op.f("ck_subscription_history_movement_type"),
        ),
        sa.CheckConstraint(
            "periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')",
            name=op.f("ck_subscription_history_periodicity"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscription.subscription_id"],
            name=op.f("fk_subscription_history_subscription_id_subscription"),
        ),
        sa.ForeignKeyConstraint(
            ["commitment_id"],
            ["billing_commitment.commitment_id"],
            name=op.f("fk_subscription_history_commitment_id_billing_commitment"),
        ),
        sa.PrimaryKeyConstraint("movement_id", name=op.f("pk_subscription_history")),
    )
    op.create_index(
        "ix_subscription_history_user",
        "subscription_history",
        ["user_id", "occurred_at", "movement_id"],
    )

    op.create_table(
        "command_idempotency",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "actor_id", "operation", "key", name=op.f("pk_command_idempotency")
        ),
    )
    op.create_index(
        op.f("ix_command_idempotency_expires_at"), "command_idempotency", ["expires_at"]
    )

    backfill(op.get_bind())


def backfill(connection: sa.Connection) -> None:
    """Relleno idempotente de las suscripciones previas: repetirlo converge al mismo estado."""
    connection.execute(
        sa.text(
            "UPDATE subscription SET periodicity = 'NONE', period_start = started_at "
            "WHERE periodicity IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE subscription AS s SET plan_offer_id = o.offer_id FROM plan_offer AS o "
            "WHERE s.plan_offer_id IS NULL AND o.plan_code = s.plan_code "
            "AND o.plan_version = s.plan_version AND o.periodicity = 'NONE'"
        )
    )
    metadata = sa.MetaData()
    history = sa.Table("subscription_history", metadata, autoload_with=connection)
    rows = connection.execute(
        sa.text(
            "SELECT subscription_id, user_id, plan_code, started_at FROM subscription "
            "WHERE origin = 'AUTO_REGISTRATION'"
        )
    ).all()
    if rows:
        connection.execute(
            postgresql.insert(history)
            .values(
                [
                    {
                        "movement_id": free_activated_movement_id(row.subscription_id),
                        "user_id": row.user_id,
                        "movement_type": "FREE_ACTIVATED",
                        "occurred_at": row.started_at,
                        "subscription_id": row.subscription_id,
                        "commitment_id": None,
                        "plan_code": row.plan_code,
                        "periodicity": "NONE",
                    }
                    for row in rows
                ]
            )
            .on_conflict_do_nothing()
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_command_idempotency_expires_at"), table_name="command_idempotency")
    op.drop_table("command_idempotency")
    op.drop_index("ix_subscription_history_user", table_name="subscription_history")
    op.drop_table("subscription_history")
    op.drop_index("ix_billing_commitment_user_status", table_name="billing_commitment")
    op.drop_table("billing_commitment")
    op.drop_constraint(op.f("ck_subscription_ended"), "subscription", type_="check")
    op.drop_constraint(op.f("ck_subscription_periodicity"), "subscription", type_="check")
    op.drop_constraint(op.f("ck_subscription_origin"), "subscription", type_="check")
    op.create_check_constraint(
        op.f("ck_subscription_origin"), "subscription", "origin IN ('AUTO_REGISTRATION')"
    )
    op.drop_constraint(op.f("ck_subscription_status"), "subscription", type_="check")
    op.create_check_constraint(op.f("ck_subscription_status"), "subscription", "status IN ('ACTIVE')")
    op.drop_constraint(
        op.f("fk_subscription_plan_offer_id_plan_offer"), "subscription", type_="foreignkey"
    )
    op.drop_column("subscription", "period_end")
    op.drop_column("subscription", "period_start")
    op.drop_column("subscription", "plan_offer_id")
    op.drop_column("subscription", "periodicity")
