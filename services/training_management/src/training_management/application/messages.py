"""Lectura validada de los eventos de `sportapp.entrenamientos.v1` para el proyector.

Se proyectan `plan.entrenamiento_creado.v1` (HU010),
`autorizacion_entrenamiento.otorgada.v1` / `.revocada.v1` y `entrenamiento.finalizado.v1`
(HU011), `entrenamiento.iniciado.v1` (HU032), `indicador.calculado.v1` (HU034) y
`plan.alimentacion_creado.v1` (HU014), `ruta.publicada.v1` y `ruta.retirada.v1`
(HU026), `plantilla_entrenamiento.publicada.v1` / `.retirada.v1` (HU028) y
`medicion.recibida.v1` / `riesgo.detectado.v1` (HU036). Cada
uno se valida campo a campo; un mensaje que no cumple el contrato es un `InvalidEvent`:
no se confirma y, agotados los reintentos, termina en la DLQ. Otro tipo de evento del
tópico es `UnsupportedEvent`: este proyector todavía no lo proyecta y se confirma sin
efectos.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from training_management.application.health_documents import (
    InvalidHealthReportDocument,
    health_report_from_document,
)
from training_management.application.ports import (
    ActiveSessionView,
    AuthorizationView,
    PlanView,
    RiskAlertView,
    SessionHistoryEntry,
    SessionView,
)
from training_management.application.route_documents import (
    InvalidRouteDocument,
    route_from_document,
)
from training_management.application.template_documents import (
    InvalidTemplateDocument,
    template_from_document,
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
from training_management.domain.health.events import EVENT_TYPE as HEALTH_REPORT_REGISTERED
from training_management.domain.health.events import SCHEMA_VERSION as HEALTH_REPORT_SCHEMA
from training_management.domain.health.model import HealthReport
from training_management.domain.measurements.model import (
    MAX_HEART_RATE,
    MAX_SAMPLES,
    MIN_HEART_RATE,
)
from training_management.domain.metrics.catalog import MAX_RESULT_SCALE, quantum
from training_management.domain.metrics.events import EVENT_TYPE as METRIC_CALCULATED
from training_management.domain.metrics.events import SCHEMA_VERSION as METRIC_SCHEMA
from training_management.domain.metrics.model import (
    CALCULATED_VERSION,
    CalculationStatus,
    MetricInput,
    MetricResult,
    TrainingMetricCalculation,
)
from training_management.domain.monitoring.events import (
    AGGREGATE_TYPE as MONITORING_AGGREGATE_TYPE,
)
from training_management.domain.monitoring.events import (
    MEASUREMENT_EVENT_TYPE,
    RISK_EVENT_TYPE,
)
from training_management.domain.monitoring.events import (
    SCHEMA_VERSION as MONITORING_SCHEMA,
)
from training_management.domain.monitoring.model import (
    RULE as RISK_RULE,
)
from training_management.domain.monitoring.model import (
    AlertSeverity,
    BatchSummary,
)
from training_management.domain.nutrition.catalog import FoodUnit
from training_management.domain.nutrition.events import (
    EVENT_TYPE as NUTRITION_PLAN_CREATED,
)
from training_management.domain.nutrition.events import (
    SCHEMA_VERSION as NUTRITION_PLAN_SCHEMA,
)
from training_management.domain.nutrition.model import (
    ITEMS_PER_MEAL_MAX,
    MEALS_MAX,
    OBJECTIVE_MAX_LENGTH,
    OBJECTIVES_MAX,
    TITLE_MAX_LENGTH,
    Meal,
    MealType,
    NutritionPlan,
    NutritionPlanStatus,
    PlanItem,
    quantity_in_range,
)
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
from training_management.domain.routes.events import (
    EVENT_TYPE_BY_STATUS as ROUTE_EVENT_BY_STATUS,
)
from training_management.domain.routes.events import (
    PUBLISHED_EVENT_TYPE as ROUTE_PUBLISHED,
)
from training_management.domain.routes.events import (
    RETIRED_EVENT_TYPE as ROUTE_RETIRED,
)
from training_management.domain.routes.events import SCHEMA_VERSION as ROUTE_SCHEMA
from training_management.domain.routes.model import Route
from training_management.domain.sessions.events import EVENT_TYPE as SESSION_FINISHED
from training_management.domain.sessions.events import SCHEMA_VERSION as SESSION_SCHEMA
from training_management.domain.sessions.events import STARTED_EVENT_TYPE as SESSION_STARTED
from training_management.domain.sessions.events import SessionStatus
from training_management.domain.templates.events import (
    EVENT_TYPE_BY_STATUS as TEMPLATE_EVENT_BY_STATUS,
)
from training_management.domain.templates.events import (
    PUBLISHED_EVENT_TYPE as TEMPLATE_PUBLISHED,
)
from training_management.domain.templates.events import (
    RETIRED_EVENT_TYPE as TEMPLATE_RETIRED,
)
from training_management.domain.templates.events import SCHEMA_VERSION as TEMPLATE_SCHEMA
from training_management.domain.templates.model import TrainingPlanTemplate


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


@dataclass(frozen=True)
class SessionStartedEvent:
    event_id: UUID
    correlation_id: UUID | None
    session: ActiveSessionView


@dataclass(frozen=True)
class MetricCalculatedEvent:
    event_id: UUID
    correlation_id: UUID | None
    calculation: TrainingMetricCalculation


@dataclass(frozen=True)
class NutritionPlanCreatedEvent:
    event_id: UUID
    correlation_id: UUID | None
    plan: NutritionPlan


@dataclass(frozen=True)
class RouteChangedEvent:
    event_id: UUID
    event_type: str
    correlation_id: UUID | None
    route: Route


@dataclass(frozen=True)
class TemplateChangedEvent:
    event_id: UUID
    event_type: str
    correlation_id: UUID | None
    template: TrainingPlanTemplate


@dataclass(frozen=True)
class MeasurementReceivedEvent:
    event_id: UUID
    correlation_id: UUID | None
    summary: BatchSummary

    @property
    def aggregate_id(self) -> UUID:
        return self.summary.session_id


@dataclass(frozen=True)
class RiskDetectedEvent:
    event_id: UUID
    correlation_id: UUID | None
    alert: RiskAlertView

    @property
    def aggregate_id(self) -> UUID:
        return self.alert.session_id


@dataclass(frozen=True)
class HealthReportRegisteredEvent:
    event_id: UUID
    correlation_id: UUID | None
    report: HealthReport

    @property
    def aggregate_id(self) -> UUID:
        return self.report.report_id


TrainingEvent = (
    PlanCreatedEvent
    | AuthorizationChangedEvent
    | SessionFinishedEvent
    | SessionStartedEvent
    | MetricCalculatedEvent
    | NutritionPlanCreatedEvent
    | RouteChangedEvent
    | TemplateChangedEvent
    | MeasurementReceivedEvent
    | RiskDetectedEvent
    | HealthReportRegisteredEvent
)


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


def _session_started(envelope: dict[str, Any]) -> SessionStartedEvent:
    payload = _payload(envelope, SESSION_SCHEMA)
    version = _envelope_version(envelope)
    session_id = _uuid(payload.get("session_id"), "session_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != session_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("status") != SessionStatus.ACTIVE.value:
        raise InvalidEvent("unknown_status")
    sport = payload.get("sport")
    if sport not in {value.value for value in Sport}:
        raise InvalidEvent("unknown_sport")
    plan_raw = payload.get("plan_id")
    return SessionStartedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        session=ActiveSessionView(
            session_id=session_id,
            athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
            plan_id=_uuid(plan_raw, "plan_id") if plan_raw is not None else None,
            sport=str(sport),
            started_at=_datetime(payload.get("started_at"), "started_at"),
            version=version,
        ),
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidEvent(f"{field}_invalid")
    return value


def _metric_input(item: object) -> MetricInput:
    if not isinstance(item, dict):
        raise InvalidEvent("inputs_invalid")
    value = item.get("value")
    # Las entradas del catálogo son enteras escalares (ADR-030).
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidEvent("inputs_invalid")
    return MetricInput(
        code=_text(item.get("code"), "inputs"),
        value=Decimal(value),
        unit=_text(item.get("unit"), "inputs"),
    )


def _metric_result(raw: object) -> MetricResult:
    if not isinstance(raw, dict):
        raise InvalidEvent("result_invalid")
    value, scale = raw.get("value"), raw.get("scale")
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not isinstance(scale, int)
        or isinstance(scale, bool)
        or not 0 <= scale <= MAX_RESULT_SCALE
    ):
        raise InvalidEvent("result_invalid")
    try:
        decimal = Decimal(str(value))
        exact = decimal.is_finite() and decimal == decimal.quantize(quantum(scale))
    except InvalidOperation:
        exact = False
    if not exact:
        raise InvalidEvent("result_invalid")
    return MetricResult(value=decimal, unit=_text(raw.get("unit"), "result"), scale=scale)


def _metric_calculated(envelope: dict[str, Any]) -> MetricCalculatedEvent:
    payload = _payload(envelope, METRIC_SCHEMA)
    # El cálculo es inmutable: cualquier otra versión no cumple el contrato (→ DLQ).
    if _envelope_version(envelope) != CALCULATED_VERSION:
        raise InvalidEvent("aggregate_version_invalid")
    calculation_id = _uuid(payload.get("calculation_id"), "calculation_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != calculation_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("status") != CalculationStatus.CONFIRMED.value:
        raise InvalidEvent("unknown_status")
    method_version = payload.get("method_version")
    if (
        not isinstance(method_version, int)
        or isinstance(method_version, bool)
        or method_version < 1
    ):
        raise InvalidEvent("method_version_invalid")
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise InvalidEvent("inputs_invalid")
    inputs = tuple(_metric_input(item) for item in raw_inputs)
    if len({item.code for item in inputs}) != len(inputs):
        raise InvalidEvent("inputs_invalid")
    return MetricCalculatedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        calculation=TrainingMetricCalculation(
            calculation_id=calculation_id,
            athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
            metric_code=_text(payload.get("metric_code"), "metric_code"),
            method_code=_text(payload.get("method_code"), "method_code"),
            method_version=method_version,
            measured_at=_datetime(payload.get("measured_at"), "measured_at"),
            calculated_at=_datetime(payload.get("calculated_at"), "calculated_at"),
            inputs=inputs,
            result=_metric_result(payload.get("result")),
            disclaimer_key=_text(payload.get("disclaimer_key"), "disclaimer_key"),
        ),
    )


_QUANTITY_PATTERN = re.compile(r"[0-9]{1,4}\.[0-9]")
_FOOD_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


def _bounded_list(value: object, field: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise InvalidEvent(f"{field}_invalid")
    return value


def _nutrition_item(raw: object) -> PlanItem:
    if not isinstance(raw, dict):
        raise InvalidEvent("items_invalid")
    food_code, quantity, unit = raw.get("food_code"), raw.get("quantity"), raw.get("unit")
    if not isinstance(food_code, str) or not _FOOD_CODE_PATTERN.fullmatch(food_code):
        raise InvalidEvent("food_code_invalid")
    if not isinstance(quantity, str) or not _QUANTITY_PATTERN.fullmatch(quantity):
        raise InvalidEvent("quantity_invalid")
    value = Decimal(quantity)
    if not quantity_in_range(value):
        raise InvalidEvent("quantity_invalid")
    if unit not in {item.value for item in FoodUnit}:
        raise InvalidEvent("unit_invalid")
    return PlanItem(food_code=food_code, quantity=value, unit=str(unit))


def _nutrition_meal(raw: object) -> Meal:
    if not isinstance(raw, dict):
        raise InvalidEvent("meals_invalid")
    meal_type = raw.get("meal_type")
    if meal_type not in {item.value for item in MealType}:
        raise InvalidEvent("meal_type_invalid")
    items = _bounded_list(raw.get("items"), "items", ITEMS_PER_MEAL_MAX)
    return Meal(
        meal_type=MealType(str(meal_type)), items=tuple(_nutrition_item(item) for item in items)
    )


def _nutrition_plan_created(envelope: dict[str, Any]) -> NutritionPlanCreatedEvent:
    payload = _payload(envelope, NUTRITION_PLAN_SCHEMA)
    version = _envelope_version(envelope)
    plan_id = _uuid(payload.get("plan_id"), "plan_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != plan_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if payload.get("version") != version:
        raise InvalidEvent("plan_version_mismatch")
    if payload.get("status") not in {item.value for item in NutritionPlanStatus}:
        raise InvalidEvent("unknown_status")
    title = payload.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= TITLE_MAX_LENGTH:
        raise InvalidEvent("title_invalid")
    objectives = _bounded_list(payload.get("objectives"), "objectives", OBJECTIVES_MAX)
    if not all(
        isinstance(item, str) and 1 <= len(item) <= OBJECTIVE_MAX_LENGTH for item in objectives
    ):
        raise InvalidEvent("objectives_invalid")
    start_date = _date(payload.get("start_date"), "start_date")
    end_date = _date(payload.get("end_date"), "end_date")
    if end_date < start_date:
        raise InvalidEvent("end_date_before_start_date")
    meals = _bounded_list(payload.get("meals"), "meals", MEALS_MAX)
    return NutritionPlanCreatedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        plan=NutritionPlan(
            plan_id=plan_id,
            athlete_id=_uuid(payload.get("athlete_id"), "athlete_id"),
            professional_id=_uuid(payload.get("professional_id"), "professional_id"),
            title=title,
            objectives=tuple(objectives),
            start_date=start_date,
            end_date=end_date,
            status=NutritionPlanStatus(str(payload["status"])),
            version=version,
            catalog_version=_text(payload.get("catalog_version"), "catalog_version"),
            meals=tuple(_nutrition_meal(meal) for meal in meals),
            created_at=_datetime(payload.get("created_at"), "created_at"),
        ),
    )


def _route_changed(envelope: dict[str, Any], event_type: str) -> RouteChangedEvent:
    payload = _payload(envelope, ROUTE_SCHEMA)
    version = _envelope_version(envelope)
    try:
        route = route_from_document(payload)
    except InvalidRouteDocument as error:
        raise InvalidEvent(error.reason) from None
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != route.route_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if route.version != version:
        raise InvalidEvent("route_version_mismatch")
    if ROUTE_EVENT_BY_STATUS[route.status] != event_type:
        raise InvalidEvent("status_mismatch")
    return RouteChangedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        event_type=event_type,
        correlation_id=_correlation(envelope),
        route=route,
    )


def _template_changed(envelope: dict[str, Any], event_type: str) -> TemplateChangedEvent:
    payload = _payload(envelope, TEMPLATE_SCHEMA)
    version = _envelope_version(envelope)
    try:
        template = template_from_document(payload)
    except InvalidTemplateDocument as error:
        raise InvalidEvent(error.reason) from None
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != template.template_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if template.version != version:
        raise InvalidEvent("template_version_mismatch")
    if TEMPLATE_EVENT_BY_STATUS[template.status] != event_type:
        raise InvalidEvent("status_mismatch")
    return TemplateChangedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        event_type=event_type,
        correlation_id=_correlation(envelope),
        template=template,
    )


def _health_report_registered(envelope: dict[str, Any]) -> HealthReportRegisteredEvent:
    """HU042 (ADR-037): `reporte_salud.registrado.v1` con el estado completo del reporte."""
    payload = _payload(envelope, HEALTH_REPORT_SCHEMA)
    version = _envelope_version(envelope)
    try:
        report = health_report_from_document({**payload, "version": version})
    except InvalidHealthReportDocument as error:
        raise InvalidEvent(error.reason) from None
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != report.report_id:
        raise InvalidEvent("aggregate_id_mismatch")
    return HealthReportRegisteredEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        report=report,
    )


_DEVICE_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
_AVERAGE_PATTERN = re.compile(r"[0-9]{2,3}\.[0-9]")


def _device_id(value: object) -> str:
    if not isinstance(value, str) or not _DEVICE_ID_PATTERN.fullmatch(value):
        raise InvalidEvent("device_id_invalid")
    return value


def _session_aggregate(envelope: dict[str, Any], payload: dict[str, Any]) -> UUID:
    session_id = _uuid(payload.get("session_id"), "session_id")
    if _uuid(envelope.get("aggregate_id"), "aggregate_id") != session_id:
        raise InvalidEvent("aggregate_id_mismatch")
    if envelope.get("aggregate_type") != MONITORING_AGGREGATE_TYPE:
        raise InvalidEvent("aggregate_type_mismatch")
    return session_id


def _measurement_received(envelope: dict[str, Any]) -> MeasurementReceivedEvent:
    """HU036: resumen de un lote; `aggregate_version` es la versión del resumen."""
    payload = _payload(envelope, MONITORING_SCHEMA)
    version = _envelope_version(envelope)
    session_id = _session_aggregate(envelope, payload)
    if payload.get("version") != version:
        raise InvalidEvent("summary_version_mismatch")
    window_from = _datetime(payload.get("window_from"), "window_from")
    window_to = _datetime(payload.get("window_to"), "window_to")
    if window_to < window_from:
        raise InvalidEvent("window_to_before_window_from")
    last_value = _int(payload.get("last_value"), "last_value", MIN_HEART_RATE, MAX_HEART_RATE)
    max_value = _int(payload.get("max_value"), "max_value", MIN_HEART_RATE, MAX_HEART_RATE)
    raw_average = payload.get("avg_value")
    if not isinstance(raw_average, str) or not _AVERAGE_PATTERN.fullmatch(raw_average):
        raise InvalidEvent("avg_value_invalid")
    average = Decimal(raw_average)
    if last_value > max_value or not MIN_HEART_RATE <= average <= max_value:
        raise InvalidEvent("summary_values_inconsistent")
    return MeasurementReceivedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        summary=BatchSummary(
            session_id=session_id,
            device_id=_device_id(payload.get("device_id")),
            window_from=window_from,
            window_to=window_to,
            samples_accepted=_int(
                payload.get("samples_accepted"), "samples_accepted", 1, MAX_SAMPLES
            ),
            last_value=last_value,
            max_value=max_value,
            avg_value=average,
            version=version,
        ),
    )


def _risk_detected(envelope: dict[str, Any]) -> RiskDetectedEvent:
    """HU036: alerta confirmada; `aggregate_version` es su número de orden en la sesión."""
    payload = _payload(envelope, MONITORING_SCHEMA)
    version = _envelope_version(envelope)
    session_id = _session_aggregate(envelope, payload)
    if payload.get("rule") != RISK_RULE:
        raise InvalidEvent("unknown_rule")
    if payload.get("severity") not in {item.value for item in AlertSeverity}:
        raise InvalidEvent("unknown_severity")
    return RiskDetectedEvent(
        event_id=_uuid(envelope.get("event_id"), "event_id"),
        correlation_id=_correlation(envelope),
        alert=RiskAlertView(
            alert_id=_uuid(payload.get("alert_id"), "alert_id"),
            session_id=session_id,
            device_id=_device_id(payload.get("device_id")),
            rule=RISK_RULE,
            severity=str(payload["severity"]),
            detected_at=_datetime(payload.get("detected_at"), "detected_at"),
            sequence=version,
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
    if event_type == SESSION_STARTED:
        return _session_started(envelope)
    return _parse_snapshot(envelope, event_type)


def _parse_snapshot(envelope: dict[str, Any], event_type: str) -> TrainingEvent:
    """Eventos de estado completo de HU034, HU014 y HU026."""
    if event_type == METRIC_CALCULATED:
        return _metric_calculated(envelope)
    if event_type == NUTRITION_PLAN_CREATED:
        return _nutrition_plan_created(envelope)
    if event_type in (ROUTE_PUBLISHED, ROUTE_RETIRED):
        return _route_changed(envelope, event_type)
    if event_type in (TEMPLATE_PUBLISHED, TEMPLATE_RETIRED):
        return _template_changed(envelope, event_type)
    if event_type in (MEASUREMENT_EVENT_TYPE, RISK_EVENT_TYPE):
        return _parse_monitoring(envelope, event_type)
    if event_type == HEALTH_REPORT_REGISTERED:
        return _health_report_registered(envelope)
    raise UnsupportedEvent(event_type)


def _parse_monitoring(envelope: dict[str, Any], event_type: str) -> TrainingEvent:
    """Eventos de HU036 (`medicion.recibida.v1` y `riesgo.detectado.v1`)."""
    if event_type == MEASUREMENT_EVENT_TYPE:
        return _measurement_received(envelope)
    return _risk_detected(envelope)
