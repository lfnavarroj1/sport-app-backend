"""HU005 (ADR-034): vista `athlete_profile_view` y deduplicación `processed_event`.

Base de lectura de `usuarios-read-projector` (CMP-001): una fila por deportista con
las secciones del perfil en JSONB (estado completo publicado en cada evento, sin
`user_id`) y su versión; `sport_habits` queda reservada para HU006. `processed_event`
deduplica por `event_id` también para `user_directory`. Solo expansión, sin backfill:
el proyector se despliega antes que los productores y reconstruye las vistas desde
los eventos. La reversión elimina ambas tablas.

Revision ID: 0003_athlete_profile_view
Revises: 0002_user_directory
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_athlete_profile_view"
down_revision: str | None = "0002_user_directory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECTIONS = ("demographic", "physiological", "sport_habits")


def upgrade() -> None:
    op.create_table(
        "athlete_profile_view",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        *(
            sa.Column(section, postgresql.JSONB(astext_type=sa.Text()), nullable=True)
            for section in SECTIONS
        ),
        *(sa.Column(f"{section}_version", sa.BigInteger(), nullable=True) for section in SECTIONS),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        *(
            sa.CheckConstraint(
                f"({section} IS NULL) = ({section}_version IS NULL)"
                f" AND ({section}_version IS NULL OR {section}_version >= 1)",
                name=op.f(f"ck_athlete_profile_view_{section}_version"),
            )
            for section in SECTIONS
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_athlete_profile_view")),
    )
    op.create_table(
        "processed_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("projection_name", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_processed_event")),
    )


def downgrade() -> None:
    op.drop_table("processed_event")
    op.drop_table("athlete_profile_view")
