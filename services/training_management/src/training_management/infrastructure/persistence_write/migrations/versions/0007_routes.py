"""HU026 (ADR-041): rutas sugeribles publicadas por organizadores.

Solo expansión (tabla nueva): compatible con la versión anterior del código. La idempotencia
de la publicación reutiliza `command_idempotency` (operación `publish_route`) y los eventos la
outbox existente. La reversión elimina la tabla y solo es segura antes de tener datos reales.

Revision ID: 0007_routes
Revises: 0006_nutrition_plans
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_routes"
down_revision: str | None = "0006_nutrition_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("distance_km", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("difficulty", sa.String(length=16), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("start_lat", sa.Double(), nullable=False),
        sa.Column("start_lng", sa.Double(), nullable=False),
        sa.Column("waypoints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('PUBLISHED', 'RETIRED')", name=op.f("ck_route_status")),
        sa.CheckConstraint(
            "sport IN ('running', 'cycling', 'swimming', 'strength', 'other')",
            name=op.f("ck_route_sport"),
        ),
        sa.CheckConstraint(
            "difficulty IN ('LOW', 'MODERATE', 'HIGH')", name=op.f("ck_route_difficulty")
        ),
        sa.CheckConstraint(
            "distance_km BETWEEN 0.1 AND 500.0", name=op.f("ck_route_distance_km")
        ),
        sa.CheckConstraint("start_lat BETWEEN -90 AND 90", name=op.f("ck_route_start_lat")),
        sa.CheckConstraint("start_lng BETWEEN -180 AND 180", name=op.f("ck_route_start_lng")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_route_version")),
        sa.CheckConstraint(
            "(status = 'RETIRED') = (retired_at IS NOT NULL)", name=op.f("ck_route_retired_at")
        ),
        sa.PrimaryKeyConstraint("route_id", name=op.f("pk_route")),
    )
    op.create_index(op.f("ix_route_organizer_id"), "route", ["organizer_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_route_organizer_id"), table_name="route")
    op.drop_table("route")
