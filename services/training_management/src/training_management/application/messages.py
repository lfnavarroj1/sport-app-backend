"""Lectura validada de los eventos de `sportapp.entrenamientos.v1` para el proyector.

Se proyectan `plan.entrenamiento_creado.v1` (HU010),
`autorizacion_entrenamiento.otorgada.v1` / `.revocada.v1` y `entrenamiento.finalizado.v1`
(HU011). Cada uno se valida campo a campo; un mensaje que no cumple el contrato es un
`InvalidEvent`: no se confirma y, agotados los reintentos, termina en la DLQ. Otro
tipo de evento del tópico es `UnsupportedEvent`: este proyector todavía no lo
proyecta y se confirma sin efectos.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from training_management.application.ports import (
    AuthorizationView,
    PlanView,
    SessionHistoryEntry,
    SessionView,
)
from training_management.domain.authorizations.events import (
    GRANTED,
    REVOKED,
    authorization_aggregate_id,
)
from training_management.domain.authorizations.events import (
    SCHEMA_VERSION as AUTHORIZATION_SCHEMA,
)
from training_management.domain.authorizations.model import AuthorizationStatus
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
from training_management.domain.sessions.events import EVENT_TYPE as SESSION_FINISHED
from training_management.domain.sessions.events import SCHEMA_VERSION as SESSION_SCHEMA
from training_management.domain.sessions.events import SessionStatus


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


@dataclass(frozen=True)
class AuthorizationChangedEvent:
    event_id: UUID
    event_type: str
    aggregate_id: UUID
    correlation_id: UUID | None
    authorization: AuthorizationView


@dataclass(frozen=True)
class SessionFinishedEvent:
    event_id: UUID
    correlation_id: UUID | None
    session: SessionHistoryEntry


TrainingEvent = PlanCreatedEvent | AuthorizationChangedEvent | SessionFinishedEvent


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


def _datetime(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidEvent(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidEvent(f"{field}_without_timezone")
    return parsed


def _envelope_version(envelope: dict[str, Any]) -> int:
    version = envelope.get("aggregate_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidEvent("aggregate_version_invalid")
    return version


def _correlation(envelope: dict[str, Any]) -> UUID | None:
    correlation = envelope.get("correlation_id")
    return _uuid(correlation, "correlation_id") if correlation else None


def _payload(envelope: dict[str, Any], schema_version: int) -> dict[str, Any]:
    if envelope.get("schema_version") != schema_version:
        raise InvalidEvent("unsupported_schema_version")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidEvent("payload_not_object")
    return payload


def _plan_created(envelope: dict[str, Any]) -> PlanCreatedEvent:
    payload = _payload(envelope, PLAN_CREATED_SCHEMA)
    version = _envelope_version(envelope)
    return PlanCreatedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        aggregate_version=version,
        correlation_id=_correlation(envelope),
        plan=_plan(payload, _uuid(envelope.get("aggregate_id"), "aggregate_id"), version),
    )


def _authorization_changed(envelope: dict[str, Any], event_type: str) -> AuthorizationChangedEvent:
    payload = _payload(envelope, AUTHORIZATION_SCHEMA)
    version = _envelope_version(envelope)
    athlete_id = _uuid(payload.get("athlete_id"), "athlete_id")
    professional_id = _uuid(payload.get("professional_id"), "professional_id")
    aggregate_id = _uuid(envelope.get("aggregate_id"), "aggregate_id")
    if aggregate_id != authorization_aggregate_id(athlete_id, professional_id):
        raise InvalidEvent("aggregate_id_mismatch")
    expected = AuthorizationStatus.ACTIVE if event_type == GRANTED else AuthorizationStatus.REVOKED
    if payload.get("status") != expected.value:
        raise InvalidEvent("status_mismatch")
    revoked_raw = payload.get("revoked_at")
    revoked_at = _datetime(revoked_raw, "revoked_at") if revoked_raw is not None else None
    if (expected is AuthorizationStatus.REVOKED) != (revoked_at is not None):
        raise InvalidEvent("revoked_at_mismatch")
    return AuthorizationChangedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        event_type=event_type,
        aggregate_id=aggregate_id,
        correlation_id=_correlation(envelope),
        authorization=AuthorizationView(
            athlete_id=athlete_id,
            professional_id=professional_id,
            status=expected.value,
            version=version,
            granted_at=_datetime(payload.get("granted_at"), "granted_at"),
            revoked_at=revoked_at,
        ),
    )


def _session_finished(envelope: dict[str, Any]) -> SessionFinishedEvent:
    payload = _payload(envelope, SESSION_SCHEMA)
    version = _envelope_version(envelope)
    session_id = _uuid(payload.get("session_id"), "session_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != session_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("status") != SessionStatus.FINISHED.value:
        raise InvalidEvent("unknown_status")
    sport = payload.get("sport")
    if sport not in {value.value for value in Sport}:
        raise InvalidEvent("unknown_sport")
    plan_raw = payload.get("plan_id")
    started_at = _datetime(payload.get("started_at"), "started_at")
    ended_at = _datetime(payload.get("ended_at"), "ended_at")
    if ended_at < started_at:
        raise InvalidEvent("ended_before_started")
    duration = payload.get("duration_seconds")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        raise InvalidEvent("duration_seconds_invalid")
    return SessionFinishedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        session=SessionHistoryEntry(
            session_id=session_id,
            athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
            plan_id=_uuid(plan_raw, "plan_id") if plan_raw is not None else None,
            sport=str(sport),
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            version=version,
        ),
    )


def parse_training_event(envelope: object) -> TrainingEvent:
    if not isinstance(envelope, dict):
        raise InvalidEvent("envelope_not_object")
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        raise InvalidEvent("event_type_missing")
    if event_type == PLAN_CREATED:
        return _plan_created(envelope)
    if event_type in (GRANTED, REVOKED):
        return _authorization_changed(envelope, event_type)
    if event_type == SESSION_FINISHED:
        return _session_finished(envelope)
    raise UnsupportedEvent(event_type)
