"""HU011: eventos de las autorizaciones creadas antes de que existieran (backfill).

HU010 guardó autorizaciones sin publicar eventos. Esta migración deja en la outbox
un `autorizacion_entrenamiento.otorgada.v1` o `.revocada.v1` por cada autorización
existente, con su estado y versión actuales, para que `entrenamientos-read-projector`
construya la vista de lectura completa. El proyector aplica un evento solo si su
versión es mayor que la proyectada, así que repetir o mezclar eventos es inofensivo.

Sin cambios de esquema. La reversión no borra los eventos: pueden estar publicados
y la proyección es idempotente.

Revision ID: 0003_authorization_backfill
Revises: 0002_training_plans
Create Date: 2026-10-12
"""

import json
from collections.abc import Sequence
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "0003_authorization_backfill"
down_revision: str | None = "0002_training_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copia congelada de `domain/authorizations/events.py` al 2026-10-12.
AUTHORIZATION_NAMESPACE = UUID("7a1d6a52-3c0e-4f43-9a7e-2b9d6c1f0e11")
BACKFILL_CAUSATION = "migration:0003_authorization_backfill"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT athlete_id, professional_id, status, granted_at, revoked_at, version, "
            "updated_at FROM training_authorization"
        )
    ).mappings()
    insert = sa.text(
        """
        INSERT INTO outbox_event (
            event_id, aggregate_type, aggregate_id, aggregate_version, event_type,
            schema_version, occurred_at, correlation_id, causation_id, payload,
            attempts, next_attempt_at
        ) VALUES (
            :event_id, 'training_authorization', :aggregate_id, :version, :event_type,
            1, :occurred_at, NULL, :causation_id, CAST(:payload AS JSONB), 0, now()
        )
        """
    )
    for row in rows:
        active = row["status"] == "ACTIVE"
        payload = {
            "athlete_id": str(row["athlete_id"]),
            "professional_id": str(row["professional_id"]),
            "status": row["status"],
            "granted_at": row["granted_at"].isoformat(),
            "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        }
        connection.execute(
            insert,
            {
                "event_id": uuid4(),
                "aggregate_id": uuid5(
                    AUTHORIZATION_NAMESPACE, f"{row['athlete_id']}:{row['professional_id']}"
                ),
                "version": row["version"],
                "event_type": "autorizacion_entrenamiento.otorgada.v1"
                if active
                else "autorizacion_entrenamiento.revocada.v1",
                "occurred_at": row["updated_at"],
                "causation_id": BACKFILL_CAUSATION,
                "payload": json.dumps(payload),
            },
        )


def downgrade() -> None:
    # Intencionalmente vacío: los eventos pueden haberse publicado ya.
    pass
