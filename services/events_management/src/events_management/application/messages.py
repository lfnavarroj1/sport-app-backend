"""Lectura validada de los eventos de `sportapp.eventos.v1` para el proyector (CMP-004).

Se proyectan `evento.creado.v1` (HU021) y `usuario.inscrito.v1` (HU041). Un mensaje que
no cumple el contrato es un `InvalidEvent`: no se confirma y, agotados los reintentos,
termina en la DLQ. Otro tipo de evento del tópico es `UnsupportedEvent`: este proyector
todavía no lo proyecta y se confirma sin efectos.

`parse_event_document` también reconstruye el evento guardado como resultado idempotente.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from events_management.domain.registrations.events import EVENT_TYPE as REGISTERED
from events_management.domain.registrations.events import (
    SCHEMA_VERSION as REGISTERED_SCHEMA,
)
from events_management.domain.registrations.model import (
    AppliedFee,
    EventRegistration,
    RegistrationStatus,
)
from events_management.domain.sport_events.countries import COUNTRY_CODES
from events_management.domain.sport_events.created import EVENT_TYPE, SCHEMA_VERSION
from events_management.domain.sport_events.model import (
    ADDRESS_MAX,
    ADDRESS_MIN,
    CAPACITY_MAX,
    CAPACITY_MIN,
    CITY_MAX,
    CITY_MIN,
    DESCRIPTION_MAX,
    DESCRIPTION_MIN,
    DISCIPLINE_MAX,
    DISCIPLINE_MIN,
    DURATION_MAX,
    DURATION_MIN,
    LOCATION_NAME_MAX,
    LOCATION_NAME_MIN,
    NAME_MAX,
    NAME_MIN,
    EventDetails,
    EventStatus,
    Location,
    SportEvent,
)


class InvalidEvent(Exception):
    """El mensaje no cumple el contrato; el motivo no incluye datos del evento."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedEvent(Exception):
    """Evento válido del tópico que este proyector no proyecta."""

    def __init__(self, event_type: str) -> None:
        super().__init__(event_type)
        self.event_type = event_type


@dataclass(frozen=True)
class EventCreatedMessage:
    message_id: UUID  # `event_id` del sobre: identifica la entrega para deduplicar.
    aggregate_version: int
    correlation_id: UUID | None
    event: SportEvent


@dataclass(frozen=True)
class RegistrationCreatedMessage:
    message_id: UUID
    aggregate_version: int
    correlation_id: UUID | None
    registration: EventRegistration


SportEventMessage = EventCreatedMessage | RegistrationCreatedMessage


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_uuid") from None


def _int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InvalidEvent(f"{field}_out_of_range")
    return value


def _text(document: dict[str, Any], field: str, minimum: int, maximum: int) -> str:
    value = document.get(field)
    if not isinstance(value, str) or value != value.strip() or not minimum <= len(value) <= maximum:
        raise InvalidEvent(f"{field}_invalid")
    return value


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise InvalidEvent(f"{field}_not_utc")
    return parsed


def _location(value: object) -> Location:
    if not isinstance(value, dict):
        raise InvalidEvent("location_not_object")
    country = value.get("country_code")
    if country not in COUNTRY_CODES:
        raise InvalidEvent("country_code_invalid")
    return Location(
        name=_text(value, "name", LOCATION_NAME_MIN, LOCATION_NAME_MAX),
        address=_text(value, "address", ADDRESS_MIN, ADDRESS_MAX),
        city=_text(value, "city", CITY_MIN, CITY_MAX),
        country_code=str(country),
    )


def parse_event_document(document: object) -> SportEvent:
    """Evento con la forma de ADR-028 (instantes en UTC); `InvalidEvent` si no la cumple."""
    if not isinstance(document, dict):
        raise InvalidEvent("payload_not_object")
    # `evento.creado.v1` y el resultado idempotente de HU021 siempre describen un borrador.
    if document.get("status") != EventStatus.DRAFT.value:
        raise InvalidEvent("unknown_status")
    time_zone = document.get("time_zone")
    if not isinstance(time_zone, str) or not time_zone:
        raise InvalidEvent("time_zone_invalid")
    duration = _int(
        document.get("duration_minutes"), "duration_minutes", DURATION_MIN, DURATION_MAX
    )
    starts_at = _instant(document.get("starts_at"), "starts_at")
    ends_at = _instant(document.get("ends_at"), "ends_at")
    if ends_at - starts_at != timedelta(minutes=duration):
        raise InvalidEvent("ends_at_mismatch")
    return SportEvent(
        event_id=_uuid(document.get("event_id"), "event_id"),
        organizer_id=_uuid(document.get("organizer_id"), "organizer_id"),
        details=EventDetails(
            name=_text(document, "name", NAME_MIN, NAME_MAX),
            description=_text(document, "description", DESCRIPTION_MIN, DESCRIPTION_MAX),
            discipline=_text(document, "discipline", DISCIPLINE_MIN, DISCIPLINE_MAX),
            location=_location(document.get("location")),
            starts_at=starts_at,
            ends_at=ends_at,
            time_zone=time_zone,
            duration_minutes=duration,
            capacity=_int(document.get("capacity"), "capacity", CAPACITY_MIN, CAPACITY_MAX),
        ),
        status=EventStatus(document["status"]),
        version=_int(document.get("version"), "version", 1, 2**62),
        created_at=_instant(document.get("created_at"), "created_at"),
        updated_at=_instant(document.get("updated_at"), "updated_at"),
    )


def _event_created(envelope: dict[str, Any]) -> EventCreatedMessage:
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise InvalidEvent("unsupported_schema_version")
    version = envelope.get("aggregate_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidEvent("aggregate_version_invalid")
    event = parse_event_document(envelope.get("payload"))
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != event.event_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if event.version != version:
        raise InvalidEvent("version_mismatch")
    correlation = envelope.get("correlation_id")
    return EventCreatedMessage(
        message_id=_uuid(envelope.get("event_id"), "event_id"),
        aggregate_version=version,
        correlation_id=_uuid(correlation, "correlation_id") if correlation else None,
        event=event,
    )


def _fee(value: object) -> AppliedFee | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("currency"), str):
        raise InvalidEvent("applied_fee_invalid")
    try:
        return AppliedFee(Decimal(str(value.get("amount"))), value["currency"])
    except InvalidOperation:
        raise InvalidEvent("applied_fee_invalid") from None


def _registration_created(envelope: dict[str, Any]) -> RegistrationCreatedMessage:
    if envelope.get("schema_version") != REGISTERED_SCHEMA:
        raise InvalidEvent("unsupported_schema_version")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidEvent("payload_not_object")
    version = envelope.get("aggregate_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidEvent("aggregate_version_invalid")
    registration_id = _uuid(payload.get("registration_id"), "registration_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != registration_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("version") != version:
        raise InvalidEvent("version_mismatch")
    if payload.get("status") not in {status.value for status in RegistrationStatus}:
        raise InvalidEvent("unknown_status")
    registered_at = _instant(payload.get("registered_at"), "registered_at")
    correlation = envelope.get("correlation_id")
    return RegistrationCreatedMessage(
        message_id=_uuid(envelope.get("event_id"), "event_id"),
        aggregate_version=version,
        correlation_id=_uuid(correlation, "correlation_id") if correlation else None,
        registration=EventRegistration(
            registration_id=registration_id,
            event_id=_uuid(payload.get("event_id"), "event_id"),
            athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
            status=RegistrationStatus(payload["status"]),
            registered_at=registered_at,
            applied_fee=_fee(payload.get("applied_fee")),
            version=version,
            created_at=registered_at,
            updated_at=registered_at,
        ),
    )


def parse_sport_event_message(envelope: object) -> SportEventMessage:
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        raise InvalidEvent("event_type_missing")
    if event_type == EVENT_TYPE:
        return _event_created(envelope)
    if event_type == REGISTERED:
        return _registration_created(envelope)
    raise UnsupportedEvent(event_type)
