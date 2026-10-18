"""HU011 (CA5): índice de las autorizaciones vigentes por profesional.

`GET /v1/me/authorized-athletes` filtra `training_authorization_view` por `professional_id`
y estado y pagina en orden `granted_at` desc, `athlete_id` desc. Solo agrega un índice
(compatible con despliegue progresivo); la reversión lo elimina.

Revision ID: 0007_authorized_athletes_index
Revises: 0006_training_metric_results
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_authorized_athletes_index"
down_revision: str | None = "0006_training_metric_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_training_authorization_view_professional_order"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "training_authorization_view",
        ["professional_id", "status", "granted_at", "athlete_id"],
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="training_authorization_view")
