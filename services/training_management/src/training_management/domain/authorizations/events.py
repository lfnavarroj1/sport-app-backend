"""Eventos de la autorización deportista → profesional (HU011, aprobados el 2026-10-12).

`autorizacion_entrenamiento.otorgada.v1` y `autorizacion_entrenamiento.revocada.v1`
llevan el estado completo de la autorización. El agregado se identifica con un
UUIDv5 determinista de la pareja, así no cambia la clave de la tabla; la versión
del agregado es la de la autorización (1 otorgar, 2 revocar, 3 reactivar...).
"""

from typing import Any
from uuid import UUID, uuid5

from training_management.domain.authorizations.model import TrainingAuthorization

GRANTED = "autorizacion_entrenamiento.otorgada.v1"
REVOKED = "autorizacion_entrenamiento.revocada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_authorization"
# Espacio de nombres fijo del UUIDv5: cambiarlo cambiaría la identidad de todos los agregados.
AUTHORIZATION_NAMESPACE = UUID("7a1d6a52-3c0e-4f43-9a7e-2b9d6c1f0e11")


def authorization_aggregate_id(athlete_id: UUID, professional_id: UUID) -> UUID:
    return uuid5(AUTHORIZATION_NAMESPACE, f"{athlete_id}:{professional_id}")


def authorization_event_type(authorization: TrainingAuthorization) -> str:
    return GRANTED if authorization.is_active else REVOKED


def authorization_payload(authorization: TrainingAuthorization) -> dict[str, Any]:
    return {
        "athlete_id": str(authorization.athlete_id),
        "professional_id": str(authorization.professional_id),
        "status": authorization.status.value,
        "granted_at": authorization.granted_at.isoformat(),
        "revoked_at": authorization.revoked_at.isoformat() if authorization.revoked_at else None,
    }
