"""Lectura validada de los eventos de `sportapp.entrenamientos.v1` para el proyector.

`plan.entrenamiento_creado.v1` se valida campo a campo; un mensaje que no cumple
el contrato es un `InvalidEvent`: no se confirma y, agotados los reintentos,
termina en la DLQ. Otro tipo de evento del tópico es `UnsupportedEvent`: este
proyector todavía no lo proyecta y se confirma sin efectos.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from training_management.application.ports import PlanView, SessionView
from training_management.domain.plans.events import EVENT_TYPE as PLAN_CREATED
from training_management.domain.plans.events import SCHEMA_VERSION as PLAN_CREATED_SCHEMA
from training_management.domain.plans.model import (
    DURATION_MINUTES_MAX,
    DURATION_MINUTES_MIN,
    INTENSITY_RPE_MAX,
    INTENSITY_RPE_MIN,
    PlanStatus,
    Sport,
)


class InvalidEvent(Exception):
    """El mensaje no cumple el contrato; el motivo no incluye datos del plan."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedEvent(Exception):
    """Evento válido del tópico que este proyector no proyecta."""

    def __init__(self, event_type: str) -> None:
        super().__init__(event_type)
        self.event_type = event_type


@dataclass(frozen=True)
class PlanCreatedEvent:
    event_id: UUID
    aggregate_version: int
    correlation_id: UUID | None
    plan: PlanView


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_uuid") from None


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_date") from None


def _int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InvalidEvent(f"{field}_out_of_range")
    return value


def _session(item: object) -> SessionView:
    if not isinstance(item, dict):
        raise InvalidEvent("session_not_object")
    sport = item.get("sport")
    if sport not in {value.value for value in Sport}:
        raise InvalidEvent("unknown_sport")
    return SessionView(
        scheduled_date=_date(item.get("scheduled_date"), "scheduled_date"),
        sport=str(sport),
        duration_minutes=_int(
            item.get("duration_minutes"),
            "duration_minutes",
            DURATION_MINUTES_MIN,
            DURATION_MINUTES_MAX,
        ),
        intensity_rpe=_int(
            item.get("intensity_rpe"), "intensity_rpe", INTENSITY_RPE_MIN, INTENSITY_RPE_MAX
        ),
    )


def _plan(payload: dict[str, Any], aggregate_id: UUID, version: int) -> PlanView:
    plan_id = _uuid(payload.get("plan_id"), "plan_id")
    if plan_id != aggregate_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("plan_version") != version:
        raise InvalidEvent("plan_version_mismatch")
    if payload.get("status") not in {value.value for value in PlanStatus}:
        raise InvalidEvent("unknown_status")
    title, objectives, sessions = (
        payload.get("title"),
        payload.get("objectives"),
        payload.get("sessions"),
    )
    if not isinstance(title, str) or not title:
        raise InvalidEvent("title_invalid")
    if not isinstance(objectives, list) or not all(isinstance(item, str) for item in objectives):
        raise InvalidEvent("objectives_invalid")
    if not isinstance(sessions, list) or not sessions:
        raise InvalidEvent("sessions_invalid")
    try:
        created_at = datetime.fromisoformat(str(payload.get("created_at")))
    except ValueError:
        raise InvalidEvent("created_at_not_datetime") from None
    return PlanView(
        plan_id=plan_id,
        athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
        professional_id=_uuid(payload.get("professional_id"), "professional_id"),
        title=title,
        objectives=tuple(objectives),
        start_date=_date(payload.get("start_date"), "start_date"),
        end_date=_date(payload.get("end_date"), "end_date"),
        status=str(payload["status"]),
        version=version,
        sessions=tuple(_session(item) for item in sessions),
        created_at=created_at,
    )


def parse_training_event(envelope: object) -> PlanCreatedEvent:
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        raise InvalidEvent("event_type_missing")
    if event_type != PLAN_CREATED:
        raise UnsupportedEvent(event_type)
    if envelope.get("schema_version") != PLAN_CREATED_SCHEMA:
        raise InvalidEvent("unsupported_schema_version")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidEvent("payload_not_object")
    version = envelope.get("aggregate_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidEvent("aggregate_version_invalid")
    correlation = envelope.get("correlation_id")
    return PlanCreatedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        aggregate_version=version,
        correlation_id=_uuid(correlation, "correlation_id") if correlation else None,
        plan=_plan(payload, _uuid(envelope.get("aggregate_id"), "aggregate_id"), version),
    )
