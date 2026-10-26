"""HU026: proyección `route_view` de las rutas sugeribles (ADR-041).

Expansión de la base de lectura sin backfill: las rutas llegan por `ruta.publicada.v1` y
`ruta.retirada.v1`, y el proyector se despliega antes o junto con el productor. Índices:
`(status, sport, city_key)` para las sugerencias (ADR-041), `(organizer_id, created_at DESC,
route_id DESC)` para `GET /v1/me/routes` y `(status, published_at DESC, route_id DESC)` para la
lista no personalizada. La reversión elimina la tabla; la base de lectura es reconstruible.

Revision ID: 0009_route_view
Revises: 0008_nutrition_plan_view
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_route_view"
down_revision: str | None = "0008_nutrition_plan_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUGGESTION_INDEX = "ix_route_view_status_sport_city"
ORGANIZER_INDEX = "ix_route_view_organizer_order"
LATEST_INDEX = "ix_route_view_status_published"


def upgrade() -> None:
    op.create_table(
        "route_view",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("city_key", sa.String(length=160), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("distance_km", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("route_id", name=op.f("pk_route_view")),
    )
    op.create_index(
        SUGGESTION_INDEX, "route_view", ["status", "sport", "city_key"], unique=False
    )
    op.create_index(
        ORGANIZER_INDEX,
        "route_view",
        ["organizer_id", sa.text("created_at DESC"), sa.text("route_id DESC")],
        unique=False,
    )
    op.create_index(
        LATEST_INDEX,
        "route_view",
        ["status", sa.text("published_at DESC"), sa.text("route_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(LATEST_INDEX, table_name="route_view")
    op.drop_index(ORGANIZER_INDEX, table_name="route_view")
    op.drop_index(SUGGESTION_INDEX, table_name="route_view")
    op.drop_table("route_view")
