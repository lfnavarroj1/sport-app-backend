"""HU001 (ADR-024): catálogo de planes `plans-2026-10-11.v1`, suscripción, inbox y outbox.

Expansión: solo tablas nuevas. La semilla es idempotente (`ON CONFLICT DO NOTHING`)
y nunca modifica una versión existente; una corrección del catálogo se entrega en
una migración nueva con otra `plan_version` y otra versión de catálogo.

Los textos provienen de `arquitectura/planes/01..04`; el dominio dueño de cada
capacidad, del mapa HU → dominio de la arquitectura. Los planes pagos quedan
definidos pero no seleccionables: su precio, moneda y periodicidad siguen
pendientes de aprobación y se guardan como nulos, sin inventarlos.

La reversión elimina las tablas y solo es segura antes de tener datos reales.

Revision ID: 0002_free_plan_catalog
Revises: 0001_baseline
Create Date: 2026-10-11
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_free_plan_catalog"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION = "plans-2026-10-11.v1"
VALID_FROM = datetime(2026, 10, 11, tzinfo=UTC)

PLANS = [
    {
        "code": "FREE",
        "name": "Gratuito",
        "description": "Permitir que todo deportista nuevo utilice las funciones esenciales de "
        "SportApp sin realizar una selección ni un pago inicial.",
        "level": 1,
        "display_order": 1,
        "status": "ACTIVE",
        "is_default_for_athlete": True,
        "is_selectable": True,
    },
    {
        "code": "INTERMEDIATE",
        "name": "Intermedio",
        "description": "Extender el plan gratuito para deportistas que requieren medición y "
        "acompañamiento durante una sesión de entrenamiento.",
        "level": 2,
        "display_order": 2,
        "status": "DEFINED",
        "is_default_for_athlete": False,
        "is_selectable": False,
    },
    {
        "code": "PREMIUM",
        "name": "Premium",
        "description": "Extender el seguimiento deportivo con acceso a servicios profesionales "
        "especializados y sesiones virtuales.",
        "level": 3,
        "display_order": 3,
        "status": "DEFINED",
        "is_default_for_athlete": False,
        "is_selectable": False,
    },
]

PLAN_VERSIONS = [
    {"plan_code": "FREE", "price": Decimal("0"), "currency": None, "periodicity": "NONE"},
    {"plan_code": "INTERMEDIATE", "price": None, "currency": None, "periodicity": None},
    {"plan_code": "PREMIUM", "price": None, "currency": None, "periodicity": None},
]

# (código, nombre visible, dominio dueño)
CAPABILITIES = [
    ("training.plan.manage", "Crear planes de entrenamiento", "training_management"),
    ("training.routes.suggest", "Sugerir rutas de entrenamiento", "training_management"),
    ("events.suggest", "Sugerir eventos deportivos", "events_management"),
    ("training.plans.suggest", "Sugerir planes de entrenamiento", "training_management"),
    ("services.support.suggest", "Sugerir acompañamiento y logística", "complementary_services"),
    ("nutrition.routines.suggest", "Sugerir rutinas de alimentación", "training_management"),
    ("integrations.strava.connect", "Conectar con Strava", "third_party_management"),
    ("training.session.record", "Iniciar y finalizar entrenamientos", "training_management"),
    ("training.dashboard.view", "Consultar tablero de entrenamiento", "training_management"),
    ("training.metrics.ftp_vo2max", "Consultar FTP y VO₂max", "training_management"),
    ("calendar.sport.view", "Consultar calendario deportivo", "events_management"),
    (
        "training.realtime.monitor",
        "Monitorear el entrenamiento en tiempo real",
        "training_management",
    ),
    ("devices.measurement.connect", "Conectar dispositivos de medición", "training_management"),
    (
        "nutrition.session.reminders",
        "Recibir recordatorios de alimentación e hidratación durante la sesión",
        "training_management",
    ),
    (
        "services.sports_medicine.access",
        "Acceder a servicios médicos y deportivos",
        "complementary_services",
    ),
    (
        "services.virtual_sessions.schedule",
        "Programar sesiones virtuales con profesionales",
        "complementary_services",
    ),
]

_FREE = [code for code, _, _ in CAPABILITIES[:11]]
_INTERMEDIATE = [*_FREE, *(code for code, _, _ in CAPABILITIES[11:14])]
_PREMIUM = [*_INTERMEDIATE, *(code for code, _, _ in CAPABILITIES[14:16])]
PLAN_CAPABILITIES = {"FREE": _FREE, "INTERMEDIATE": _INTERMEDIATE, "PREMIUM": _PREMIUM}


def _create_schema() -> None:
    op.create_table(
        "plan",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("display_order", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_default_for_athlete", sa.Boolean(), nullable=False),
        sa.Column("is_selectable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'DEFINED')", name=op.f("ck_plan_status")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_plan")),
    )
    op.create_table(
        "plan_version",
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("periodicity", sa.String(length=16), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_code"], ["plan.code"], name=op.f("fk_plan_version_plan_code_plan")
        ),
        sa.PrimaryKeyConstraint("plan_code", "version", name=op.f("pk_plan_version")),
    )
    op.create_table(
        "capability",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_domain", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE')", name=op.f("ck_capability_status")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_capability")),
    )
    op.create_table(
        "plan_version_capability",
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("capability_code", sa.String(length=64), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["capability_code"],
            ["capability.code"],
            name=op.f("fk_plan_version_capability_capability_code_capability"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_code", "plan_version"],
            ["plan_version.plan_code", "plan_version.version"],
            name=op.f("fk_plan_version_capability_plan_code_plan_version"),
        ),
        sa.PrimaryKeyConstraint(
            "plan_code",
            "plan_version",
            "capability_code",
            name=op.f("pk_plan_version_capability"),
        ),
    )
    op.create_table(
        "subscription",
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "origin IN ('AUTO_REGISTRATION')", name=op.f("ck_subscription_origin")
        ),
        sa.CheckConstraint("status IN ('ACTIVE')", name=op.f("ck_subscription_status")),
        sa.ForeignKeyConstraint(
            ["plan_code", "plan_version"],
            ["plan_version.plan_code", "plan_version.version"],
            name=op.f("fk_subscription_plan_code_plan_version"),
        ),
        sa.PrimaryKeyConstraint("subscription_id", name=op.f("pk_subscription")),
    )
    op.create_index(op.f("ix_subscription_user_id"), "subscription", ["user_id"])
    op.create_index(
        "uq_subscription_active_user",
        "subscription",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "inbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_inbox_event")),
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
    op.create_table(
        "catalog_metadata",
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("catalog_version", name=op.f("pk_catalog_metadata")),
    )


def seed_catalog(connection: sa.Connection) -> None:
    """Precarga idempotente: repetirla no duplica ni altera versiones existentes."""
    now = datetime.now(UTC)
    metadata = sa.MetaData()
    plan = sa.Table("plan", metadata, autoload_with=connection)
    plan_version = sa.Table("plan_version", metadata, autoload_with=connection)
    capability = sa.Table("capability", metadata, autoload_with=connection)
    plan_version_capability = sa.Table(
        "plan_version_capability", metadata, autoload_with=connection
    )
    catalog_metadata = sa.Table("catalog_metadata", metadata, autoload_with=connection)

    def insert(table: sa.Table, rows: list[dict[str, object]]) -> None:
        connection.execute(postgresql.insert(table).values(rows).on_conflict_do_nothing())

    insert(plan, [{**row, "created_at": now} for row in PLANS])
    insert(
        plan_version,
        [
            {**row, "version": 1, "catalog_version": CATALOG_VERSION, "valid_from": VALID_FROM}
            for row in PLAN_VERSIONS
        ],
    )
    insert(
        capability,
        [
            {"code": code, "name": name, "owner_domain": owner, "status": "ACTIVE"}
            for code, name, owner in CAPABILITIES
        ],
    )
    insert(
        plan_version_capability,
        [
            {"plan_code": plan_code, "plan_version": 1, "capability_code": code}
            for plan_code, codes in PLAN_CAPABILITIES.items()
            for code in codes
        ],
    )
    insert(catalog_metadata, [{"catalog_version": CATALOG_VERSION, "applied_at": now}])


def upgrade() -> None:
    _create_schema()
    seed_catalog(op.get_bind())


def downgrade() -> None:
    op.drop_table("catalog_metadata")
    op.drop_index(
        "idx_outbox_pending",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_event")
    op.drop_table("inbox_event")
    op.drop_index(
        "uq_subscription_active_user",
        table_name="subscription",
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_index(op.f("ix_subscription_user_id"), table_name="subscription")
    op.drop_table("subscription")
    op.drop_table("plan_version_capability")
    op.drop_table("capability")
    op.drop_table("plan_version")
    op.drop_table("plan")
