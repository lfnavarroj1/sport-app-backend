"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.dashboard.model import (
    DashboardPeriod,
    LatestMetric,
    SessionBucket,
)
from training_management.domain.metrics.model import TrainingMetricCalculation
from training_management.domain.nutrition.catalog import FoodCatalog
from training_management.domain.nutrition.model import NutritionPlan, NutritionRestrictions
from training_management.domain.plans.model import Sport, TrainingPlan
from training_management.domain.routes.model import Route
from training_management.domain.routes.suggestions import SuggestionContext
from training_management.domain.sessions.model import TrainingSession


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Caller:
    """Identidad verificada a partir del access token; nunca del cuerpo de la solicitud."""

    user_id: UUID
    actor_type: str


class PermissionPolicy(Protocol):
    """Contrato versionado `actor-permissions` (ADR-025; v2 desde HU026)."""

    def allows(self, actor_type: str, permission: str) -> bool: ...


class CapabilityChecker(Protocol):
    """Capacidades vigentes del deportista en Afiliación (ADR-024).

    Lanza `CapabilitiesUnavailable` si no puede evaluarlas.
    """

    def has_capability(self, user_id: UUID, capability: str) -> bool: ...


@dataclass(frozen=True)
class SessionView:
    scheduled_date: date
    sport: str
    duration_minutes: int
    intensity_rpe: int


@dataclass(frozen=True)
class PlanView:
    """Representación del plan para respuestas y para la vista de lectura (sin datos de perfil)."""

    plan_id: UUID
    athlete_id: UUID
    professional_id: UUID
    title: str
    objectives: tuple[str, ...]
    start_date: date
    end_date: date
    status: str
    version: int
    sessions: tuple[SessionView, ...]
    created_at: datetime

    @classmethod
    def from_plan(cls, plan: TrainingPlan) -> "PlanView":
        return cls(
            plan_id=plan.plan_id,
            athlete_id=plan.athlete_id,
            professional_id=plan.professional_id,
            title=plan.title,
            objectives=plan.objectives,
            start_date=plan.start_date,
            end_date=plan.end_date,
            status=plan.status.value,
            version=plan.version,
            sessions=tuple(
                SessionView(
                    scheduled_date=session.scheduled_date,
                    sport=session.sport.value,
                    duration_minutes=session.duration_minutes,
                    intensity_rpe=session.intensity_rpe,
                )
                for session in plan.sessions
            ),
            created_at=plan.created_at,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "plan_id": str(self.plan_id),
            "athlete_id": str(self.athlete_id),
            "professional_id": str(self.professional_id),
            "title": self.title,
            "objectives": list(self.objectives),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "status": self.status,
            "version": self.version,
            "sessions": [
                {
                    "scheduled_date": session.scheduled_date.isoformat(),
                    "sport": session.sport,
                    "duration_minutes": session.duration_minutes,
                    "intensity_rpe": session.intensity_rpe,
                }
                for session in self.sessions
            ],
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PlanView":
        return cls(
            plan_id=UUID(document["plan_id"]),
            athlete_id=UUID(document["athlete_id"]),
            professional_id=UUID(document["professional_id"]),
            title=document["title"],
            objectives=tuple(document["objectives"]),
            start_date=date.fromisoformat(document["start_date"]),
            end_date=date.fromisoformat(document["end_date"]),
            status=document["status"],
            version=int(document["version"]),
            sessions=tuple(
                SessionView(
                    scheduled_date=date.fromisoformat(item["scheduled_date"]),
                    sport=item["sport"],
                    duration_minutes=int(item["duration_minutes"]),
                    intensity_rpe=int(item["intensity_rpe"]),
                )
                for item in document["sessions"]
            ),
            created_at=datetime.fromisoformat(document["created_at"]),
        )


@dataclass(frozen=True)
class OutboxMessage:
    """Evento pendiente de publicar; se confirma en la misma transacción del agregado."""

    event_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class IdempotencyRecord:
    key: UUID
    operation: str
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class Transaction(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class PlanningUnitOfWork(Transaction, Protocol):
    """Transacción local de HU010: plan, outbox e idempotencia juntas."""

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def has_active_authorization(self, athlete_id: UUID, professional_id: UUID) -> bool:
        """Bloquea la autorización en modo compartido hasta el fin de la transacción: una
        revocación concurrente espera a que el plan se confirme, o se confirma antes y el
        plan se rechaza."""
        ...

    def add_plan(self, plan: TrainingPlan) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


class AuthorizationUnitOfWork(Transaction, Protocol):
    """Transacción local de la autorización deportista → profesional."""

    def get_for_update(
        self, athlete_id: UUID, professional_id: UUID
    ) -> TrainingAuthorization | None: ...

    def add(self, authorization: TrainingAuthorization) -> None: ...

    def update(self, authorization: TrainingAuthorization) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None:
        """Traduce una inserción concurrente de la misma pareja a `AuthorizationConflict`."""
        ...


class PlanReader(Protocol):
    """Vista de lectura de planes (base de lectura, CQRS)."""

    def get(self, plan_id: UUID) -> PlanView | None: ...


@dataclass(frozen=True)
class AuthorizationView:
    """Estado proyectado de la autorización deportista → profesional (HU011)."""

    athlete_id: UUID
    professional_id: UUID
    status: str
    version: int
    granted_at: datetime
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"


@dataclass(frozen=True)
class SessionHistoryEntry:
    """Sesión terminada en el historial de lectura (HU011); sin datos de salud."""

    session_id: UUID
    athlete_id: UUID
    plan_id: UUID | None
    sport: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    version: int


@dataclass(frozen=True)
class HistoryPosition:
    """Posición del cursor: última sesión entregada en el orden `ended_at`, `session_id`."""

    ended_at: datetime
    session_id: UUID


@dataclass(frozen=True)
class HistoryQuery:
    athlete_id: UUID
    ended_from: datetime | None
    ended_before: datetime | None
    sport: str | None
    after: HistoryPosition | None
    limit: int


class HistoryReader(Protocol):
    def list(self, query: HistoryQuery) -> list[SessionHistoryEntry]:
        """Hasta `limit` sesiones, de la más reciente a la más antigua, estrictamente después
        de `after` en el orden (`ended_at` desc, `session_id` desc)."""
        ...


class AuthorizationReader(Protocol):
    def get(self, athlete_id: UUID, professional_id: UUID) -> AuthorizationView | None: ...


@dataclass(frozen=True)
class AuthorizedAthleteEntry:
    """Deportista con autorización vigente hacia el profesional (HU011, CA5). Solo
    identificadores: el nombre y el correo los agrega el BFF desde Usuarios."""

    athlete_id: UUID
    authorized_at: datetime


@dataclass(frozen=True)
class AuthorizedAthletePosition:
    """Posición del cursor: último deportista entregado en el orden `authorized_at`,
    `athlete_id` descendentes."""

    authorized_at: datetime
    athlete_id: UUID


@dataclass(frozen=True)
class AuthorizedAthletesQuery:
    professional_id: UUID
    after: AuthorizedAthletePosition | None
    limit: int


class AuthorizedAthleteReader(Protocol):
    def list(self, query: AuthorizedAthletesQuery) -> list[AuthorizedAthleteEntry]:
        """Hasta `limit` autorizaciones vigentes (`ACTIVE`) del profesional, de la más reciente
        a la más antigua, estrictamente después de `after`."""
        ...


@dataclass(frozen=True)
class CommandRecord:
    """Resultado de un comando de HU032 o HU034, con clave aislada por deportista y operación."""

    actor_id: UUID
    operation: str
    key: UUID
    request_fingerprint: str
    resource_id: UUID
    response: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class SessionUnitOfWork(Transaction, Protocol):
    """Transacción local de HU032: sesión, outbox e idempotencia juntas."""

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None: ...

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def add_command(self, record: CommandRecord) -> None: ...

    def get_session(self, session_id: UUID, *, for_update: bool = False) -> TrainingSession | None:
        """Con `for_update`, bloquea la fila hasta el fin de la transacción (finalización)."""
        ...

    def plan_allows(self, plan_id: UUID, athlete_id: UUID, sport: Sport) -> bool:
        """El plan existe, está `ASSIGNED` al deportista y contiene una sesión del deporte."""
        ...

    def add_session(self, session: TrainingSession) -> None: ...

    def update_session(self, session: TrainingSession) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None:
        """Traduce la clave duplicada a `IdempotencyKeyConflict`, el `session_id` duplicado a
        `SessionIdConflict` y una segunda sesión activa a `ActiveSessionExists`."""
        ...


@dataclass(frozen=True)
class ActiveSessionView:
    """Sesión activa proyectada (HU032) a partir de `entrenamiento.iniciado.v1`."""

    session_id: UUID
    athlete_id: UUID
    plan_id: UUID | None
    sport: str
    started_at: datetime
    version: int


class ActiveSessionReader(Protocol):
    def current(self, athlete_id: UUID) -> ActiveSessionView | None:
        """La sesión activa proyectada más reciente del deportista."""
        ...


@dataclass(frozen=True)
class PlannedSessionEntry:
    """Sesión planificada de un plan asignado (HU035), proyectada desde el plan de HU010."""

    session_id: UUID
    plan_id: UUID
    athlete_id: UUID
    plan_title: str
    scheduled_date: date
    sport: str
    duration_minutes: int
    intensity_rpe: int


@dataclass(frozen=True)
class PlannedSessionPosition:
    """Posición del cursor en el orden `scheduled_date`, `plan_id`, `session_id`."""

    scheduled_date: date
    plan_id: UUID
    session_id: UUID


@dataclass(frozen=True)
class PlannedSessionQuery:
    athlete_id: UUID
    date_from: date
    date_to: date
    after: PlannedSessionPosition | None
    limit: int


class PlannedSessionReader(Protocol):
    def list(self, query: PlannedSessionQuery) -> list[PlannedSessionEntry]:
        """Hasta `limit` sesiones de planes `ASSIGNED` del deportista con fecha en el rango
        (inclusivo), estrictamente después de `after` en el orden ascendente."""
        ...


class MetricUnitOfWork(Transaction, Protocol):
    """Transacción local de HU034: cálculo, entradas, outbox e idempotencia juntas."""

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None: ...

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def add_command(self, record: CommandRecord) -> None: ...

    def get_calculation(self, calculation_id: UUID) -> TrainingMetricCalculation | None: ...

    def add_calculation(self, calculation: TrainingMetricCalculation) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None:
        """Traduce la clave duplicada a `IdempotencyKeyConflict` y el `calculation_id`
        duplicado a `CalculationIdConflict`."""
        ...


@dataclass(frozen=True)
class MetricResultPosition:
    """Posición del cursor: último resultado entregado en el orden `measured_at`,
    `calculation_id` (ambos descendentes)."""

    measured_at: datetime
    calculation_id: UUID


@dataclass(frozen=True)
class MetricResultQuery:
    athlete_id: UUID
    metric_code: str | None
    after: MetricResultPosition | None
    limit: int


class MetricResultReader(Protocol):
    """Proyección `training_metric_result` (HU034, lectura CQRS)."""

    def list(self, query: MetricResultQuery) -> list[TrainingMetricCalculation]:
        """Hasta `limit` resultados del deportista, del más reciente al más antiguo,
        estrictamente después de `after` en el orden (`measured_at`, `calculation_id`) desc."""
        ...


class DuplicateDelivery(Exception):
    """Otra entrega del mismo evento confirmó primero su registro en `processed_event`."""


class ProjectionUnitOfWork(Transaction, Protocol):
    """Transacción local del proyector en la base de lectura."""

    def is_processed(self, event_id: UUID) -> bool: ...

    def projected_version(self, plan_id: UUID) -> int | None: ...

    def add_plan_view(self, view: PlanView, projected_at: datetime) -> None: ...

    def save_planned_sessions(self, view: PlanView, projected_at: datetime) -> None:
        """HU035: inserta o reemplaza las sesiones del plan en `training_plan_session_view`."""
        ...

    def authorization_version(self, athlete_id: UUID, professional_id: UUID) -> int | None:
        """Versión proyectada; bloquea la fila para aplicar las versiones en serie."""
        ...

    def save_authorization_view(self, view: AuthorizationView, projected_at: datetime) -> None:
        """Inserta o reemplaza el estado proyectado de la pareja."""
        ...

    def session_version(self, session_id: UUID) -> int | None: ...

    def save_session_entry(self, entry: SessionHistoryEntry, projected_at: datetime) -> None:
        """Inserta o reemplaza la sesión terminada en el historial."""
        ...

    def active_session_version(self, session_id: UUID) -> int | None:
        """Versión de la sesión activa proyectada; bloquea la fila."""
        ...

    def save_active_session(self, view: ActiveSessionView, projected_at: datetime) -> None: ...

    def delete_active_session(self, session_id: UUID) -> None: ...

    def metric_result(self, calculation_id: UUID) -> TrainingMetricCalculation | None:
        """HU034: resultado proyectado del cálculo; bloquea la fila."""
        ...

    def add_metric_result(
        self, calculation: TrainingMetricCalculation, projected_at: datetime
    ) -> None: ...

    def nutrition_plan_version(self, plan_id: UUID) -> int | None:
        """HU014: versión proyectada del plan de alimentación; bloquea la fila."""
        ...

    def save_nutrition_plan_view(self, plan: NutritionPlan, projected_at: datetime) -> None:
        """HU014: inserta o reemplaza el estado completo del plan en `nutrition_plan_view`."""
        ...

    def route_version(self, route_id: UUID) -> int | None:
        """HU026: versión proyectada de la ruta; bloquea la fila."""
        ...

    def save_route_view(self, route: Route, projected_at: datetime) -> None:
        """HU026: inserta o reemplaza el estado completo de la ruta en `route_view`."""
        ...

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un `event_id` ya registrado por otra entrega a `DuplicateDelivery`."""
        ...


class FoodCatalogReader(Protocol):
    """HU014: catálogo de alimentos vigente (`food_catalog_item`, dato de referencia de la
    base de escritura). Se lee fuera de cualquier transacción del comando."""

    def load(self) -> FoodCatalog: ...


class NutritionRestrictionsReader(Protocol):
    """HU014: restricciones del deportista en Usuarios (ADR-035), fuera de la transacción.

    Lanza `NutritionRestrictionsUnavailable` ante caída, timeout o respuesta inesperada.
    """

    def get(self, athlete_id: UUID) -> NutritionRestrictions: ...


class NutritionUnitOfWork(Transaction, Protocol):
    """Transacción local de HU014: plan de alimentación, outbox e idempotencia juntas."""

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def has_active_authorization(self, athlete_id: UUID, professional_id: UUID) -> bool:
        """Bloquea la autorización en modo compartido hasta el fin de la transacción (como en
        HU010): una revocación concurrente espera al plan o se confirma antes y lo rechaza."""
        ...

    def add_nutrition_plan(self, plan: NutritionPlan) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada; traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


@dataclass(frozen=True)
class NutritionPlanPosition:
    """Posición del cursor: último plan entregado en el orden `created_at`, `plan_id` desc."""

    created_at: datetime
    plan_id: UUID


@dataclass(frozen=True)
class NutritionPlanQuery:
    athlete_id: UUID
    after: NutritionPlanPosition | None
    limit: int


class NutritionPlanReader(Protocol):
    """Proyección `nutrition_plan_view` (HU014, lectura CQRS)."""

    def get(self, plan_id: UUID) -> NutritionPlan | None: ...

    def list(self, query: NutritionPlanQuery) -> list[NutritionPlan]:
        """Hasta `limit` planes del deportista, del más reciente al más antiguo,
        estrictamente después de `after` en el orden (`created_at`, `plan_id`) desc."""
        ...


class DashboardReader(Protocol):
    """HU033 (ADR-043): agregados del tablero desde la base de lectura.

    Cada consulta lanza `DashboardSourceUnavailable` si la base de lectura no responde; el
    tablero informa esa fuente como `UNAVAILABLE` y conserva la otra.
    """

    def session_buckets(self, athlete_id: UUID, period: DashboardPeriod) -> list[SessionBucket]:
        """Sesiones terminadas en `[from, to)` agrupadas por deporte, semana ISO en la zona
        del `from` y fuente."""
        ...

    def latest_metrics(self, athlete_id: UUID, period: DashboardPeriod) -> list[LatestMetric]:
        """Último resultado por indicador con `measured_at` en `[from, to)`."""
        ...


class RouteUnitOfWork(Transaction, Protocol):
    """Transacción local de HU026: ruta, outbox e idempotencia (`command_idempotency`)."""

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None: ...

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None: ...

    def add_command(self, record: CommandRecord) -> None: ...

    def get_route_for_update(self, route_id: UUID) -> Route | None:
        """Bloquea la fila hasta el fin de la transacción (retiro)."""
        ...

    def add_route(self, route: Route) -> None: ...

    def update_route(self, route: Route) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None:
        """Traduce la clave duplicada a `IdempotencyKeyConflict`."""
        ...


@dataclass(frozen=True)
class RoutePosition:
    """Última ruta entregada en el orden `created_at` desc, `route_id` desc."""

    created_at: datetime
    route_id: UUID


@dataclass(frozen=True)
class MyRoutesQuery:
    organizer_id: UUID
    limit: int
    after: RoutePosition | None


@dataclass(frozen=True)
class RouteSuggestionQuery:
    context: SuggestionContext
    limit: int


class RouteReader(Protocol):
    """Proyección `route_view` (HU026, lectura CQRS)."""

    def list_by_organizer(self, query: MyRoutesQuery) -> list[Route]:
        """Rutas del organizador en cualquier estado, en orden `created_at` desc, `route_id`
        desc, estrictamente después de `after`."""
        ...

    def suggest(self, query: RouteSuggestionQuery) -> list[Route]:
        """Rutas `PUBLISHED`. Con deportes: las de esos deportes, ciudad coincidente primero,
        luego `distance_km` y `route_id` ascendentes. Sin deportes: las últimas publicadas
        (`published_at` desc, `route_id` desc). Como mucho `limit`."""
        ...
