"""Lectura validada de `usuario.registrado.v1` (contrato publicado por Usuarios).

Solo se extrae lo que Afiliación necesita (`user_id` y `actor_type`); el resto del
payload no se conserva. Un mensaje que no cumple el contrato es un `InvalidEvent`:
no se confirma y, agotados los reintentos, termina en la DLQ del consumidor.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from affiliation_management.domain.subscriptions.model import KNOWN_ACTOR_TYPES

USER_REGISTERED = "usuario.registrado.v1"
USER_REGISTERED_SCHEMA_VERSION = 1


class InvalidEvent(Exception):
    """El mensaje no cumple el contrato; el motivo no incluye datos personales."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RegisteredUser:
    event_id: UUID
    user_id: UUID
    actor_type: str
    correlation_id: UUID | None


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_uuid") from None


def parse_user_registered(envelope: object) -> RegisteredUser:
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    if envelope.get("event_type") != USER_REGISTERED:
        raise InvalidEvent("unexpected_event_type")
    if envelope.get("schema_version") != USER_REGISTERED_SCHEMA_VERSION:
        raise InvalidEvent("unsupported_schema_version")
    payload: Any = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidEvent("payload_not_object")
    event_id = _uuid(envelope.get("event_id"), "event_id")
    user_id = _uuid(payload.get("user_id"), "user_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != user_id:
        raise InvalidEvent("aggregate_id_mismatch")
    actor_type = payload.get("actor_type")
    if actor_type not in KNOWN_ACTOR_TYPES:
        raise InvalidEvent("unknown_actor_type")
    correlation = envelope.get("correlation_id")
    return RegisteredUser(
        event_id=event_id,
        user_id=user_id,
        actor_type=str(actor_type),
        correlation_id=_uuid(correlation, "correlation_id") if correlation else None,
    )
