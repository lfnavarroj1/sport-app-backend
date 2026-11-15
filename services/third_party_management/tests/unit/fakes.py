"""Dobles en memoria para probar HU031 sin infraestructura.

La unidad de trabajo aplica los cambios solo al confirmar y valida las mismas unicidades
que PostgreSQL: la clave de idempotencia, una corrida pendiente por deportista y una
cuenta de Strava `ACTIVE` por atleta externo. El proveedor es el simulador de Strava con
el adaptador HTTPX real, el cifrado y el `state` reales (no tocan red ni base de datos).
"""

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sportapp_platform.tokens import AccessTokenVerifier, public_jwk
from third_party_management.application.errors import (
    AthleteAlreadyLinked,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    IdempotencyKeyConflict,
    SyncRunConflict,
)
from third_party_management.application.ports import (
    IdempotencyRecord,
    OAuthState,
    OutboxMessage,
    ProviderTokens,
    StravaContext,
)
from third_party_management.domain.activities import ImportedActivity
from third_party_management.domain.connections import (
    AccountStatus,
    ExternalAccount,
)
from third_party_management.domain.synchronization import SyncRun, SyncStatus
from third_party_management.infrastructure.oauth_state import HmacStateCodec
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.infrastructure.strava.client import (
    StravaCredentials,
    StravaHttpGateway,
)
from third_party_management.infrastructure.token_cipher import (
    AesGcmTokenSealer,
    derive_state_key,
)
from third_party_management.testing.strava_simulator import StravaSimulator

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
CAPABILITY = "integrations.strava.connect"
CONSENT_VERSION = "strava-consent-2026-10.v1"
REDIRECT_URI = "https://bff-mobile.sportapp.test/v1/integrations/strava/callback"
CLIENT_ID = "4242"

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SIGNER = Ed25519PrivateKey.generate()
VERIFIER = AccessTokenVerifier(
    {"keys": [public_jwk(SIGNER.public_key())]}, issuer=ISSUER, audience=AUDIENCE
)


def token(user_id: UUID, actor_type: str | None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
    }
    if actor_type is not None:
        claims["actor_type"] = actor_type
    return jwt.encode(
        claims, SIGNER, algorithm="EdDSA", headers={"kid": public_jwk(SIGNER.public_key())["kid"]}
    )


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakeCapabilities:
    def __init__(self, granted: dict[UUID, set[str]] | None = None) -> None:
        self.granted = granted or {}
        self.unavailable = False
        self.slow = False
        self.calls: list[tuple[UUID, str]] = []

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        self.calls.append((user_id, capability))
        if self.slow:
            raise CapabilitiesTimeout()
        if self.unavailable:
            raise CapabilitiesUnavailable()
        return capability in self.granted.get(user_id, set())


class NoSleep:
    """Registra las esperas del reintento sin dormir."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class ManualMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def strava_key() -> bytes:
    return os.urandom(32)


def gateway(
    simulator: StravaSimulator,
    *,
    breaker: CircuitBreaker | None = None,
    sleep: NoSleep | None = None,
) -> StravaHttpGateway:
    return StravaHttpGateway(
        simulator.client(),
        StravaCredentials(
            client_id=CLIENT_ID,
            client_secret=os.urandom(16).hex(),
            redirect_uri=REDIRECT_URI,
        ),
        breaker=breaker or CircuitBreaker(),
        sleep=sleep or NoSleep(),
        jitter=lambda low, high: 0.0,
    )


def strava_context(
    simulator: StravaSimulator,
    *,
    key: bytes | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: NoSleep | None = None,
) -> StravaContext:
    key = key or strava_key()
    return StravaContext(
        gateway=gateway(simulator, breaker=breaker, sleep=sleep),
        sealer=AesGcmTokenSealer(key),
        states=HmacStateCodec(derive_state_key(key)),
        consent_version=CONSENT_VERSION,
    )


def linked_account(
    context: StravaContext,
    user_id: UUID,
    *,
    access: str,
    refresh: str,
    expires_at: datetime,
    athlete_id: str = "12345",
    now: datetime = NOW,
) -> ExternalAccount:
    return ExternalAccount.link(
        current=None,
        user_id=user_id,
        external_athlete_id=athlete_id,
        tokens=context.sealer.seal(user_id, ProviderTokens(access, refresh, expires_at)),
        scopes=("read", "activity:read"),
        consent_version=CONSENT_VERSION,
        now=now,
    )


@dataclass
class InMemoryStore:
    accounts: dict[UUID, ExternalAccount] = field(default_factory=dict)
    states: dict[str, OAuthState] = field(default_factory=dict)
    runs: dict[UUID, SyncRun] = field(default_factory=dict)
    run_workers: dict[UUID, str] = field(default_factory=dict)
    idempotency: dict[tuple[UUID, str, UUID], IdempotencyRecord] = field(default_factory=dict)
    imported: dict[str, ImportedActivity] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    commits: int = 0


class InMemoryUnitOfWork:
    def __init__(
        self, store: InMemoryStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._reset()

    def _reset(self) -> None:
        self._accounts: dict[UUID, ExternalAccount] = {}
        self._states_added: dict[str, OAuthState] = {}
        self._states_deleted: set[str] = set()
        self._runs: dict[UUID, SyncRun] = {}
        self._new_runs: set[UUID] = set()
        self._idempotency: dict[tuple[UUID, str, UUID], IdempotencyRecord] = {}
        self._idempotency_deleted: set[tuple[UUID, str, UUID]] = set()
        self._imported: dict[str, ImportedActivity] = {}
        self._outbox: list[OutboxMessage] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._reset()

    # OAuth
    def add_oauth_state(self, state: OAuthState) -> None:
        self._states_added[state.state] = state

    def take_oauth_state(self, state: str) -> OAuthState | None:
        if state in self._states_deleted:
            return None
        found = self._states_added.get(state) or self.store.states.get(state)
        self._states_deleted.add(state)
        return found

    def delete_oauth_states(self, user_id: UUID, created_before: datetime | None) -> None:
        for value in list(self.store.states.values()):
            if value.user_id == user_id and (
                created_before is None or value.created_at < created_before
            ):
                self._states_deleted.add(value.state)

    # Vinculación
    def get_account(self, user_id: UUID, *, for_update: bool = False) -> ExternalAccount | None:
        return self._accounts.get(user_id) or self.store.accounts.get(user_id)

    def active_owner_of(self, external_athlete_id: str) -> UUID | None:
        for account in self.store.accounts.values():
            if (
                account.external_athlete_id == external_athlete_id
                and account.status is AccountStatus.ACTIVE
            ):
                return account.user_id
        return None

    def save_account(self, account: ExternalAccount) -> None:
        self._accounts[account.user_id] = account

    # Corridas
    def _all_runs(self) -> dict[UUID, SyncRun]:
        return {**self.store.runs, **self._runs}

    def latest_run(self, user_id: UUID) -> SyncRun | None:
        runs = [run for run in self._all_runs().values() if run.user_id == user_id]
        return max(runs, key=lambda run: (run.requested_at, run.sync_run_id), default=None)

    def get_run(self, sync_run_id: UUID) -> SyncRun | None:
        return self._all_runs().get(sync_run_id)

    def pending_runs(self, user_id: UUID) -> list[SyncRun]:
        return [
            run for run in self._all_runs().values() if run.user_id == user_id and run.is_pending
        ]

    def add_run(self, run: SyncRun) -> None:
        self._runs[run.sync_run_id] = run
        self._new_runs.add(run.sync_run_id)

    def save_run(self, run: SyncRun) -> None:
        self._runs[run.sync_run_id] = run

    def lock_owned_run(self, sync_run_id: UUID, worker_id: str) -> SyncRun | None:
        run = self.store.runs.get(sync_run_id)
        if run is None or run.status is not SyncStatus.RUNNING:
            return None
        return run if self.store.run_workers.get(sync_run_id) == worker_id else None

    # Idempotencia
    def find_idempotency(
        self, user_id: UUID, operation: str, key: UUID
    ) -> IdempotencyRecord | None:
        return self.store.idempotency.get((user_id, operation, key))

    def delete_idempotency(self, user_id: UUID, operation: str, key: UUID) -> None:
        self._idempotency_deleted.add((user_id, operation, key))

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency[(record.user_id, record.operation, record.key)] = record

    # Actividades y outbox
    def last_imported_start(self, user_id: UUID) -> datetime | None:
        starts = [
            item.started_at for item in self.store.imported.values() if item.user_id == user_id
        ]
        return max(starts, default=None)

    def get_imported(self, external_activity_id: str) -> ImportedActivity | None:
        return self._imported.get(external_activity_id) or self.store.imported.get(
            external_activity_id
        )

    def save_imported(self, activity: ImportedActivity, *, new: bool) -> None:
        self._imported[activity.external_activity_id] = activity

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        store = self.store
        for key in self._idempotency_deleted:
            store.idempotency.pop(key, None)
        if any(key in store.idempotency for key in self._idempotency):
            raise IdempotencyKeyConflict()
        for run_id in self._new_runs:
            user_id = self._runs[run_id].user_id
            if any(run.user_id == user_id and run.is_pending for run in store.runs.values()):
                raise SyncRunConflict()
        for account in self._accounts.values():
            owner = self.active_owner_of(account.external_athlete_id)
            if account.status is AccountStatus.ACTIVE and owner not in (None, account.user_id):
                raise AthleteAlreadyLinked()
        for name in self._states_deleted:
            store.states.pop(name, None)
        store.states.update(self._states_added)
        store.accounts.update(self._accounts)
        store.runs.update(self._runs)
        for run in self._runs.values():
            if not run.is_pending:
                store.run_workers.pop(run.sync_run_id, None)
        store.idempotency.update(self._idempotency)
        store.imported.update(self._imported)
        store.outbox.extend(self._outbox)
        store.commits += 1


class InMemoryClaimer:
    def __init__(self, store: InMemoryStore, clock: FixedClock) -> None:
        self.store = store
        self.clock = clock

    def claim(self, worker_id: str, lease_seconds: float) -> SyncRun | None:
        queued = sorted(
            (run for run in self.store.runs.values() if run.status is SyncStatus.QUEUED),
            key=lambda run: run.requested_at,
        )
        if not queued:
            return None
        run = replace(queued[0], status=SyncStatus.RUNNING, started_at=self.clock.now())
        self.store.runs[run.sync_run_id] = run
        self.store.run_workers[run.sync_run_id] = worker_id
        return run
