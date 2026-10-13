"""HU035: vista `training_plan_session_view` para el calendario del deportista (ADR-031).

Expansión de la base de lectura con backfill desde `training_plan_view`: cada sesión del
JSONB `sessions` se copia con su posición y el mismo identificador determinista que usa
el proyector (`planned_session_id`), así que reconstruir desde los eventos da el mismo
resultado. La reversión elimina la vista; la base de lectura es reconstruible.

Revision ID: 0005_training_plan_session_view
Revises: 0004_active_training_session
Create Date: 2026-10-12
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

from training_management.domain.plans.events import planned_session_id

revision: str = "0005_training_plan_session_view"
down_revision: str | None = "0004_active_training_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    view = op.create_table(
        "training_plan_session_view",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("plan_title", sa.String(length=120), nullable=False),
        sa.Column("plan_status", sa.String(length=16), nullable=False),
        sa.Column("plan_version", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("intensity_rpe", sa.SmallInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_training_plan_session_view")),
    )
    op.create_index(
        "ix_training_plan_session_view_calendar",
        "training_plan_session_view",
        ["athlete_id", "scheduled_date", "plan_id", "session_id"],
        unique=False,
    )
    plans = op.get_bind().execute(
        sa.text(
            "SELECT plan_id, athlete_id, title, status, version, sessions FROM training_plan_view"
        )
    )
    now = datetime.now(UTC)
    rows = [
        {
            "session_id": planned_session_id(plan.plan_id, position),
            "plan_id": plan.plan_id,
            "athlete_id": plan.athlete_id,
            "plan_title": plan.title,
            "plan_status": plan.status,
            "plan_version": plan.version,
            "position": position,
            "scheduled_date": date.fromisoformat(session["scheduled_date"]),
            "sport": session["sport"],
            "duration_minutes": int(session["duration_minutes"]),
            "intensity_rpe": int(session["intensity_rpe"]),
            "projected_at": now,
        }
        for plan in plans
        for position, session in enumerate(plan.sessions)
    ]
    if rows:
        op.bulk_insert(view, rows)


def downgrade() -> None:
    op.drop_index(
        "ix_training_plan_session_view_calendar", table_name="training_plan_session_view"
    )
    op.drop_table("training_plan_session_view")
