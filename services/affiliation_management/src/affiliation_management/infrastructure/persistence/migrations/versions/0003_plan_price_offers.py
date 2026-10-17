"""HU001 (ADR-024 y ADR-036): catálogo `plans-2026-10-13.v1` con las ofertas de precio.

Expansión sin tocar `0002_free_plan_catalog`:

- tabla `plan_offer` (oferta por versión de plan y periodicidad, `NUMERIC(12,2)` en COP);
- `catalog_metadata.migration_revision`, para auditar qué migración instaló cada catálogo;
- versión 2 de `FREE`, `INTERMEDIATE` y `PREMIUM` en `plans-2026-10-13.v1`, con las mismas
  capacidades materializadas y sus ofertas. Las versiones 1 no se modifican: `FREE` v1 ya
  tiene suscripciones y las conserva; los deportistas nuevos reciben `FREE` v2;
- `INTERMEDIATE` y `PREMIUM` pasan a activos y seleccionables (ADR-036).

Las siete ofertas son las de ADR-036 («nueve» en su texto cuenta las celdas «No aplica» del
plan gratuito); la del plan gratuito va en COP 0 con periodicidad `NONE` (decisión del
2026-10-17). Los importes son precios de referencia antes de impuestos; SportApp no cobra en
el MVP. La semilla es idempotente (`ON CONFLICT DO NOTHING` y actualizaciones que convergen):
repetirla no duplica ni altera versiones existentes. Se ejecuta en el Job de migración del
despliegue, antes de habilitar las réplicas; ningún proceso escribe catálogo al arrancar.

La reversión solo es segura antes de que existan suscripciones a las versiones 2.

Revision ID: 0003_plan_price_offers
Revises: 0002_free_plan_catalog
Create Date: 2026-10-17
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_plan_price_offers"
down_revision: str | None = "0002_free_plan_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION = "plans-2026-10-13.v1"
PREVIOUS_CATALOG_VERSION = "plans-2026-10-11.v1"
PREVIOUS_REVISION = "0002_free_plan_catalog"
PLAN_VERSION = 2
VALID_FROM = datetime(2026, 10, 13, tzinfo=UTC)
CURRENCY = "COP"
# Identificadores deterministas: la misma oferta tiene el mismo `offer_id` en todo ambiente.
OFFER_NAMESPACE = uuid.UUID("6f3e9a52-0c1d-5b8e-9a47-2d6c1f0b8e31")

FREE_TERMS = (
    "Asignación automática al registrarse como deportista. Sin costo y sin periodicidad."
)
PAID_TERMS = (
    "Precios de referencia en COP antes de impuestos, comisiones y costos comerciales, sin "
    "descuento por periodicidad. El cambio de plan es inmediato y sin prorrateo. SportApp no "
    "procesa el pago en el MVP: el compromiso de pago es informativo (PENDING_EXTERNAL)."
)

PLAN_VERSIONS = [
    {"plan_code": "FREE", "price": Decimal("0"), "periodicity": "NONE", "terms": FREE_TERMS},
    {"plan_code": "INTERMEDIATE", "price": None, "periodicity": None, "terms": PAID_TERMS},
    {"plan_code": "PREMIUM", "price": None, "periodicity": None, "terms": PAID_TERMS},
]

# (plan, periodicidad, precio en COP, meses del periodo)
OFFERS = [
    ("FREE", "NONE", Decimal("0"), None),
    ("INTERMEDIATE", "MONTHLY", Decimal("2400"), 1),
    ("INTERMEDIATE", "QUARTERLY", Decimal("7200"), 3),
    ("INTERMEDIATE", "ANNUAL", Decimal("28800"), 12),
    ("PREMIUM", "MONTHLY", Decimal("4800"), 1),
    ("PREMIUM", "QUARTERLY", Decimal("14400"), 3),
    ("PREMIUM", "ANNUAL", Decimal("57600"), 12),
]
PAID_PLANS = ("INTERMEDIATE", "PREMIUM")


def offer_id(plan_code: str, periodicity: str) -> uuid.UUID:
    return uuid.uuid5(OFFER_NAMESPACE, f"{plan_code}:{PLAN_VERSION}:{periodicity}")


def _create_schema() -> None:
    op.create_table(
        "plan_offer",
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("periodicity", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("duration_months", sa.SmallInteger(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "periodicity IN ('NONE', 'MONTHLY', 'QUARTERLY', 'ANNUAL')",
            name=op.f("ck_plan_offer_periodicity"),
        ),
        sa.CheckConstraint(
            "(periodicity = 'NONE' AND duration_months IS NULL)"
            " OR (periodicity = 'MONTHLY' AND duration_months = 1)"
            " OR (periodicity = 'QUARTERLY' AND duration_months = 3)"
            " OR (periodicity = 'ANNUAL' AND duration_months = 12)",
            name=op.f("ck_plan_offer_duration"),
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_plan_offer_price")),
        sa.CheckConstraint("currency = 'COP'", name=op.f("ck_plan_offer_currency")),
        sa.ForeignKeyConstraint(
            ["plan_code", "plan_version"],
            ["plan_version.plan_code", "plan_version.version"],
            name=op.f("fk_plan_offer_plan_code_plan_version"),
        ),
        sa.PrimaryKeyConstraint("offer_id", name=op.f("pk_plan_offer")),
        sa.UniqueConstraint(
            "plan_code",
            "plan_version",
            "periodicity",
            name=op.f("uq_plan_offer_plan_code"),
        ),
    )
    op.add_column(
        "catalog_metadata",
        sa.Column("migration_revision", sa.String(length=32), nullable=True),
    )


def seed_catalog(connection: sa.Connection) -> None:
    """Precarga idempotente de `plans-2026-10-13.v1`: repetirla converge al mismo estado."""
    now = datetime.now(UTC)
    metadata = sa.MetaData()
    plan = sa.Table("plan", metadata, autoload_with=connection)
    plan_version = sa.Table("plan_version", metadata, autoload_with=connection)
    plan_version_capability = sa.Table(
        "plan_version_capability", metadata, autoload_with=connection
    )
    plan_offer = sa.Table("plan_offer", metadata, autoload_with=connection)
    catalog_metadata = sa.Table("catalog_metadata", metadata, autoload_with=connection)

    def insert(table: sa.Table, rows: list[dict[str, object]]) -> None:
        connection.execute(postgresql.insert(table).values(rows).on_conflict_do_nothing())

    connection.execute(
        sa.update(plan)
        .where(plan.c.code.in_(PAID_PLANS))
        .values(status="ACTIVE", is_selectable=True)
    )
    insert(
        plan_version,
        [
            {
                **row,
                "version": PLAN_VERSION,
                "catalog_version": CATALOG_VERSION,
                "valid_from": VALID_FROM,
                "currency": CURRENCY,
            }
            for row in PLAN_VERSIONS
        ],
    )
    # Mismas capacidades que la versión 1 de cada plan (ADR-036: «las capacidades no cambian»).
    previous = connection.execute(
        sa.select(plan_version_capability.c.plan_code, plan_version_capability.c.capability_code)
        .where(plan_version_capability.c.plan_version == 1)
    ).all()
    insert(
        plan_version_capability,
        [
            {"plan_code": plan_code, "plan_version": PLAN_VERSION, "capability_code": code}
            for plan_code, code in previous
        ],
    )
    insert(
        plan_offer,
        [
            {
                "offer_id": offer_id(plan_code, periodicity),
                "plan_code": plan_code,
                "plan_version": PLAN_VERSION,
                "periodicity": periodicity,
                "price": price,
                "currency": CURRENCY,
                "duration_months": months,
                "valid_from": VALID_FROM,
                "created_at": now,
            }
            for plan_code, periodicity, price, months in OFFERS
        ],
    )
    connection.execute(
        sa.update(catalog_metadata)
        .where(
            catalog_metadata.c.catalog_version == PREVIOUS_CATALOG_VERSION,
            catalog_metadata.c.migration_revision.is_(None),
        )
        .values(migration_revision=PREVIOUS_REVISION)
    )
    insert(
        catalog_metadata,
        [{"catalog_version": CATALOG_VERSION, "applied_at": now, "migration_revision": revision}],
    )


def upgrade() -> None:
    _create_schema()
    seed_catalog(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    op.drop_table("plan_offer")
    connection.execute(
        sa.text("DELETE FROM catalog_metadata WHERE catalog_version = :version"),
        {"version": CATALOG_VERSION},
    )
    op.drop_column("catalog_metadata", "migration_revision")
    connection.execute(
        sa.text("DELETE FROM plan_version_capability WHERE plan_version = :version"),
        {"version": PLAN_VERSION},
    )
    connection.execute(
        sa.text("DELETE FROM plan_version WHERE version = :version"), {"version": PLAN_VERSION}
    )
    connection.execute(
        sa.text(
            "UPDATE plan SET status = 'DEFINED', is_selectable = false WHERE code IN :codes"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": list(PAID_PLANS)},
    )
