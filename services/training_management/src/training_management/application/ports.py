"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import date, datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.plans.model import Sport, TrainingPlan
from training_management.domain.sessions.model import TrainingSession


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Caller:
    """Identidad verificada a partir del access token; nunca del cuerpo de la solicitud."""

    user_id: UUID
    actor_type: str


class PermissionPolicy(Protocol):
    """Contrato versionado `actor-permissions.v1` (ADR-025)."""

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
class CommandRecord:
    """Resultado de un comando de HU032, con clave aislada por deportista y operación."""

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


class DuplicateDelivery(Exception):
    """Otra entrega del mismo evento confirmó primero su registro en `processed_event`."""


class ProjectionUnitOfWork(Transaction, Protocol):
    """Transacción local del proyector en la base de lectura."""

    def is_processed(self, event_id: UUID) -> bool: ...

    def projected_version(self, plan_id: UUID) -> int | None: ...

    def add_plan_view(self, view: PlanView, projected_at: datetime) -> None: ...

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

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None: ...

    def commit(self) -> None:
        """Traduce un `event_id` ya registrado por otra entrega a `DuplicateDelivery`."""
        ...
