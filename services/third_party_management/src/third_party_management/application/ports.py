"""Puertos de la aplicación de HU031: identidad, capacidades, reloj, proveedor externo
neutral, cifrado de tokens, `state` OAuth y persistencia.

`StravaGateway` es la interfaz neutral del adaptador (ADR-007): la aplicación no conoce URLs,
cabeceras ni formatos de Strava, salvo el registro de actividad que normaliza el dominio
(`strava-activity.v1`) y que nunca sale del servicio.
"""

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from third_party_management.domain.activities import ImportedActivity
from third_party_management.domain.connections import ExternalAccount, SealedTokens
from third_party_management.domain.synchronization import SyncRun


@dataclass(frozen=True)
class Caller:
    """Identidad verificada a partir del access token; nunca del cuerpo de la solicitud."""

    user_id: UUID
    actor_type: str


class CapabilityChecker(Protocol):
    def has_capability(self, user_id: UUID, capability: str) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class ProviderTokens:
    """Tokens en claro: solo existen en memoria, dentro de este servicio."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ProviderGrant:
    tokens: ProviderTokens
    external_athlete_id: str


@dataclass(frozen=True)
class ActivityPage:
    """`records` son los registros del proveedor sin interpretar; `quota_exhausted` indica
    que la cuota de alguna ventana quedó agotada tras esta página."""

    records: tuple[object, ...] = field(repr=False)
    quota_exhausted: bool = False


class StravaGateway(Protocol):
    """Adaptador del proveedor. Toda falla se informa como `ProviderFailure`."""

    def authorization_url(self, state: str) -> str: ...

    def exchange_code(self, code: str) -> ProviderGrant: ...

    def refresh(self, refresh_token: str) -> ProviderTokens: ...

    def deauthorize(self, access_token: str) -> None: ...

    def list_activities(
        self, access_token: str, *, after: datetime | None, page: int, per_page: int
    ) -> ActivityPage: ...


class TokenSealer(Protocol):
    """AES-256-GCM con datos asociados al deportista y al tipo de token."""

    def seal(self, user_id: UUID, tokens: ProviderTokens) -> SealedTokens: ...

    def open(self, user_id: UUID, sealed: SealedTokens) -> ProviderTokens: ...


class StateCodec(Protocol):
    """`state` antifalsificación: aleatorio + HMAC ligado al deportista."""

    def issue(self, user_id: UUID) -> str: ...

    def well_formed(self, state: str) -> bool: ...

    def verify(self, state: str, user_id: UUID) -> bool: ...


@dataclass(frozen=True)
class StravaContext:
    """Lo que exige la configuración completa de Strava; sin ella las operaciones que lo
    necesitan responden `503`."""

    gateway: StravaGateway
    sealer: TokenSealer
    states: StateCodec
    consent_version: str


@dataclass(frozen=True)
class OAuthState:
    state: str
    user_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class IdempotencyRecord:
    user_id: UUID
    operation: str
    key: UUID
    result_id: UUID
    created_at: datetime


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


class UnitOfWork(Protocol):
    """Transacción local de la base `terceros`. Lo no confirmado con `commit` se descarta."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    # Estado OAuth (un solo uso).
    def add_oauth_state(self, state: OAuthState) -> None: ...

    def take_oauth_state(self, state: str) -> OAuthState | None: ...

    def delete_oauth_states(self, user_id: UUID, created_before: datetime | None) -> None: ...

    # Vinculación.
    def get_account(self, user_id: UUID, *, for_update: bool = False) -> ExternalAccount | None: ...

    def active_owner_of(self, external_athlete_id: str) -> UUID | None: ...

    def save_account(self, account: ExternalAccount) -> None: ...

    # Corridas.
    def latest_run(self, user_id: UUID) -> SyncRun | None: ...

    def get_run(self, sync_run_id: UUID) -> SyncRun | None: ...

    def pending_runs(self, user_id: UUID) -> list[SyncRun]: ...

    def add_run(self, run: SyncRun) -> None: ...

    def save_run(self, run: SyncRun) -> None: ...

    def lock_owned_run(self, sync_run_id: UUID, worker_id: str) -> SyncRun | None: ...

    # Idempotencia.
    def find_idempotency(
        self, user_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, user_id: UUID, operation: str, key: UUID) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    # Actividades importadas y outbox.
    def last_imported_start(self, user_id: UUID) -> datetime | None: ...

    def get_imported(self, external_activity_id: str) -> ImportedActivity | None: ...

    def save_imported(self, activity: ImportedActivity, *, new: bool) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def commit(self) -> None: ...


class SyncRunClaimer(Protocol):
    def claim(self, worker_id: str, lease_seconds: float) -> SyncRun | None:
        """Reclama una corrida `QUEUED` (o `RUNNING` con arrendamiento vencido) con
        `FOR UPDATE SKIP LOCKED`, la pasa a `RUNNING` y confirma."""
        ...
