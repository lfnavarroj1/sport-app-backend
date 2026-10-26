"""Dobles en memoria para probar casos de uso sin infraestructura."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from users_management.application.commands.login import Login
from users_management.application.commands.patch_sport_habits import PatchSportHabits
from users_management.application.commands.refresh_session import RefreshSession
from users_management.application.commands.register_user import RegisterUser
from users_management.application.commands.revoke_session import RevokeSession
from users_management.application.commands.update_demographic_profile import (
    UpdateDemographicProfile,
)
from users_management.application.commands.update_nutrition_habits import UpdateNutritionHabits
from users_management.application.commands.update_physiological_profile import (
    UpdatePhysiologicalProfile,
)
from users_management.application.commands.update_sport_habits import UpdateSportHabits
from users_management.application.errors import (
    IdempotencyKeyConflict,
    ReadModelUnavailable,
    SectionAlreadyExists,
)
from users_management.application.ports import (
    AthleteProfileView,
    IdempotencyRecord,
    IssuedAccessToken,
    OutboxMessage,
    StoredProfileSection,
)
from users_management.domain.account.errors import EmailAlreadyRegistered
from users_management.domain.account.model import AccountStatus, ActorType, UserAccount
from users_management.domain.authentication.model import (
    FailureWindow,
    LoginThrottlePolicy,
    RefreshToken,
)
from users_management.domain.profile.events import ProfileSection

CURRENT_POLICIES = "2026-10"
DEFAULT_PASSWORD = "contraseña-muy-secreta"


@dataclass
class InMemoryStore:
    accounts: dict[UUID, UserAccount] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)


class InMemoryUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas unicidades que PostgreSQL."""

    def __init__(self, store: InMemoryStore, fail_on_commit: Exception | None = None) -> None:
        self.store = store
        self.fail_on_commit = fail_on_commit
        self._accounts: list[UserAccount] = []
        self._outbox: list[OutboxMessage] = []
        self._idempotency: list[IdempotencyRecord] = []
        self._deleted: list[UUID] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._accounts, self._outbox, self._idempotency, self._deleted = [], [], [], []

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get(key)

    def delete_idempotency(self, key: UUID) -> None:
        self._deleted.append(key)

    def email_exists(self, email: str) -> bool:
        return any(account.email == email for account in self.store.accounts.values())

    def add_account(self, account: UserAccount) -> None:
        self._accounts.append(account)

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency.append(record)

    def commit(self) -> None:
        if self.fail_on_commit is not None:
            raise self.fail_on_commit
        for account in self._accounts:
            if self.email_exists(account.email):
                raise EmailAlreadyRegistered()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        for record in self._idempotency:
            if record.key in self.store.idempotency:
                raise IdempotencyKeyConflict()
        for account in self._accounts:
            self.store.accounts[account.user_id] = account
        self.store.outbox.extend(self._outbox)
        for record in self._idempotency:
            self.store.idempotency[record.key] = record


class FakeHasher:
    def __init__(self) -> None:
        self.calls = 0

    def hash(self, password: str) -> str:
        self.calls += 1
        return f"$fake$hash-{self.calls}"


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class InMemoryAuthStore:
    accounts: dict[UUID, UserAccount] = field(default_factory=dict)
    failures: dict[str, FailureWindow] = field(default_factory=dict)
    refresh_tokens: dict[UUID, RefreshToken] = field(default_factory=dict)

    def add_account(self, account: UserAccount) -> UserAccount:
        self.accounts[account.user_id] = account
        return account

    def token_by_hash(self, token_hash: str) -> RefreshToken | None:
        return next((t for t in self.refresh_tokens.values() if t.token_hash == token_hash), None)


class InMemoryAuthUnitOfWork:
    """Aplica las escrituras solo al confirmar, como la transacción real."""

    def __init__(self, store: InMemoryAuthStore, throttle: LoginThrottlePolicy) -> None:
        self.store = store
        self.throttle = throttle
        self._pending: list[Callable[[], None]] = []
        self.locked_families: list[UUID] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._pending = []

    def find_account_by_email(self, email: str) -> UserAccount | None:
        return next((a for a in self.store.accounts.values() if a.email == email), None)

    def get_account(self, user_id: UUID) -> UserAccount | None:
        return self.store.accounts.get(user_id)

    def find_failure_window(self, throttle_key: str) -> FailureWindow | None:
        return self.store.failures.get(throttle_key)

    def record_failure(self, throttle_key: str, now: datetime, window: timedelta) -> FailureWindow:
        updated = self.throttle.after_failure(self.store.failures.get(throttle_key), now)
        self._pending.append(lambda: self.store.failures.__setitem__(throttle_key, updated))
        return updated

    def clear_failures(self, throttle_key: str) -> None:
        self._pending.append(lambda: self.store.failures.pop(throttle_key, None))

    def add_refresh_token(self, token: RefreshToken) -> None:
        self._pending.append(lambda: self.store.refresh_tokens.__setitem__(token.token_id, token))

    def find_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        return self.store.token_by_hash(token_hash)

    def mark_refresh_token_used(self, token_id: UUID, used_at: datetime, replaced_by: UUID) -> None:
        def apply() -> None:
            token = self.store.refresh_tokens[token_id]
            self.store.refresh_tokens[token_id] = replace(token, used_at=used_at)

        self._pending.append(apply)

    def lock_refresh_family(self, family_id: UUID) -> None:
        self.locked_families.append(family_id)

    def revoke_refresh_family(self, family_id: UUID, revoked_at: datetime) -> None:
        def apply() -> None:
            for token_id, token in list(self.store.refresh_tokens.items()):
                if token.family_id == family_id and token.revoked_at is None:
                    self.store.refresh_tokens[token_id] = replace(token, revoked_at=revoked_at)

        self._pending.append(apply)

    def commit(self) -> None:
        for apply in self._pending:
            apply()
        self._pending = []


class FakeVerifier:
    """Acepta la contraseña si el hash es `$fake$<contraseña>`; registra cada verificación."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def verify(self, password_hash: str | None, password: str) -> bool:
        self.calls.append(password_hash)
        return password_hash == f"$fake${password}"


class FakeTokenIssuer:
    def __init__(self, ttl: timedelta = timedelta(minutes=15)) -> None:
        self.ttl = ttl
        self.issued: list[tuple[UUID, str]] = []

    def issue(self, user_id: UUID, actor_type: str, now: datetime) -> IssuedAccessToken:
        self.issued.append((user_id, actor_type))
        return IssuedAccessToken(token=f"access-{len(self.issued)}", expires_at=now + self.ttl)


THROTTLE = LoginThrottlePolicy(max_failures=5, window=timedelta(minutes=15))
REFRESH_TTL = timedelta(days=7)


def account(
    email: str = "ana@example.com",
    password: str = DEFAULT_PASSWORD,
    status: AccountStatus = AccountStatus.ACTIVE,
) -> UserAccount:
    created = datetime(2026, 10, 1, tzinfo=UTC)
    return replace(
        UserAccount.register(
            user_id=uuid4(),
            full_name="Ana Pérez",
            email=email,
            actor_type=ActorType.ATHLETE,
            password_hash=f"$fake${password}",
            accepted_policies_version=CURRENT_POLICIES,
            now=created,
        ),
        status=status,
    )


def login_with(
    store: InMemoryAuthStore,
    verifier: FakeVerifier | None = None,
    issuer: FakeTokenIssuer | None = None,
    clock: FixedClock | None = None,
) -> Login:
    return Login(
        unit_of_work=lambda: InMemoryAuthUnitOfWork(store, THROTTLE),
        password_verifier=verifier or FakeVerifier(),
        token_issuer=issuer or FakeTokenIssuer(),
        clock=clock or FixedClock(),
        throttle=THROTTLE,
        refresh_token_ttl=REFRESH_TTL,
    )


def refresh_with(
    store: InMemoryAuthStore,
    issuer: FakeTokenIssuer | None = None,
    clock: FixedClock | None = None,
) -> RefreshSession:
    return RefreshSession(
        unit_of_work=lambda: InMemoryAuthUnitOfWork(store, THROTTLE),
        token_issuer=issuer or FakeTokenIssuer(),
        clock=clock or FixedClock(),
        refresh_token_ttl=REFRESH_TTL,
    )


def revoke_with(store: InMemoryAuthStore, clock: FixedClock | None = None) -> RevokeSession:
    return RevokeSession(
        unit_of_work=lambda: InMemoryAuthUnitOfWork(store, THROTTLE), clock=clock or FixedClock()
    )


def register_user_with(
    store: InMemoryStore,
    hasher: FakeHasher | None = None,
    clock: FixedClock | None = None,
) -> RegisterUser:
    return RegisterUser(
        unit_of_work=lambda: InMemoryUnitOfWork(store),
        password_hasher=hasher or FakeHasher(),
        clock=clock or FixedClock(),
        current_policies_version=CURRENT_POLICIES,
    )


# --- HU005: perfil del deportista ---

CONSENT_VERSION = "health-test.v1"


@dataclass
class InMemoryProfileStore:
    sections: dict[tuple[ProfileSection, UUID], StoredProfileSection] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    idempotency: dict[UUID, IdempotencyRecord] = field(default_factory=dict)


class InMemoryProfileUnitOfWork:
    """Aplica los cambios solo al confirmar y valida las mismas unicidades que PostgreSQL.

    `before_commit` simula una solicitud concurrente que confirma justo antes.
    """

    def __init__(
        self,
        store: InMemoryProfileStore,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._sections: list[tuple[StoredProfileSection, bool]] = []
        self._outbox: list[OutboxMessage] = []
        self._idempotency: list[IdempotencyRecord] = []
        self._deleted: list[UUID] = []
        self.locked: list[tuple[ProfileSection, UUID]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._sections, self._outbox, self._idempotency, self._deleted = [], [], [], []

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get(key)

    def delete_idempotency(self, key: UUID) -> None:
        self._deleted.append(key)

    def lock_section(self, section: ProfileSection, user_id: UUID) -> StoredProfileSection | None:
        self.locked.append((section, user_id))
        return self.store.sections.get((section, user_id))

    def save_section(self, stored: StoredProfileSection, *, created: bool) -> None:
        self._sections.append((stored, created))

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency.append(record)

    def commit(self) -> None:
        if self.before_commit is not None:
            self.before_commit()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        for record in self._idempotency:
            if record.key in self.store.idempotency:
                raise IdempotencyKeyConflict()
        for stored, created in self._sections:
            if created and (stored.section, stored.user_id) in self.store.sections:
                raise SectionAlreadyExists()
        for stored, _ in self._sections:
            self.store.sections[(stored.section, stored.user_id)] = stored
        self.store.outbox.extend(self._outbox)
        for record in self._idempotency:
            self.store.idempotency[record.key] = record


class InMemoryProfileReader:
    """Proyección simulada: devuelve lo que la prueba deje en `views`."""

    def __init__(self) -> None:
        self.views: dict[UUID, AthleteProfileView] = {}
        self.unavailable = False

    def get(self, user_id: UUID) -> AthleteProfileView | None:
        if self.unavailable:
            raise ReadModelUnavailable()
        return self.views.get(user_id)


def update_demographic_with(
    store: InMemoryProfileStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> UpdateDemographicProfile:
    return UpdateDemographicProfile(
        unit_of_work=lambda: InMemoryProfileUnitOfWork(store, before_commit),
        clock=clock or FixedClock(),
    )


def update_physiological_with(
    store: InMemoryProfileStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> UpdatePhysiologicalProfile:
    return UpdatePhysiologicalProfile(
        unit_of_work=lambda: InMemoryProfileUnitOfWork(store, before_commit),
        clock=clock or FixedClock(),
        current_consent_version=CONSENT_VERSION,
    )


def update_sport_habits_with(
    store: InMemoryProfileStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> UpdateSportHabits:
    return UpdateSportHabits(
        unit_of_work=lambda: InMemoryProfileUnitOfWork(store, before_commit),
        clock=clock or FixedClock(),
    )


def patch_sport_habits_with(
    store: InMemoryProfileStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> PatchSportHabits:
    return PatchSportHabits(
        unit_of_work=lambda: InMemoryProfileUnitOfWork(store, before_commit),
        clock=clock or FixedClock(),
    )


def update_nutrition_habits_with(
    store: InMemoryProfileStore,
    clock: FixedClock | None = None,
    before_commit: Callable[[], None] | None = None,
) -> UpdateNutritionHabits:
    return UpdateNutritionHabits(
        unit_of_work=lambda: InMemoryProfileUnitOfWork(store, before_commit),
        clock=clock or FixedClock(),
        current_consent_version=CONSENT_VERSION,
    )


def nutrition_habits_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo sintético de HU009 (sin `expected_version`: alta), con las listas fuera del
    orden del catálogo para comprobar la normalización."""
    values: dict[str, Any] = {
        "consent": {"version": CONSENT_VERSION},
        "diet_pattern": "VEGETARIAN",
        "restrictions": ["LOW_SUGAR", "GLUTEN_FREE"],
        "allergies": ["SESAME", "PEANUTS"],
        "meals_per_day": 4,
        "hydration_liters_per_day": "2.5",
    }
    values.update(overrides)
    return values


def sport_habits_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo sintético del ejemplo de ADR-034 con dos deportes (sin `expected_version`)."""
    values: dict[str, Any] = {
        "sports": [
            {
                "sport": "swimming",
                "frequency_per_week": 2,
                "session_duration_minutes": 60,
                "intensity": "LOW",
                "level": "BEGINNER",
            },
            {
                "sport": "running",
                "frequency_per_week": 3,
                "session_duration_minutes": 45,
                "intensity": "MODERATE",
                "level": "INTERMEDIATE",
                "years_practicing": 4,
            },
        ],
        "availability": {"days": ["SAT", "MON", "WED"], "time_slots": ["EVENING", "MORNING"]},
        "primary_goal": "HEALTH",
    }
    values.update(overrides)
    return values


def demographic_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo sintético del ejemplo de ADR-034 (sin `expected_version`: alta)."""
    values: dict[str, Any] = {
        "birth_date": "1992-04-18",
        "gender": "FEMALE",
        "country_code": "CO",
        "city": "Bogotá",
        "marital_status": "SINGLE",
        "occupation": "Ingeniera",
        "income_range": "2_4_SMMLV",
    }
    values.update(overrides)
    return values


def physiological_body(**overrides: Any) -> dict[str, Any]:
    """Cuerpo sintético del ejemplo de ADR-034, medido 1 h antes de `FixedClock`."""
    values: dict[str, Any] = {
        "consent": {"version": CONSENT_VERSION},
        "measured_at": "2026-10-07T11:00:00Z",
        "weight_kg": "68.5",
        "height_cm": 172,
        "resting_heart_rate_bpm": 58,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": "36.6",
    }
    values.update(overrides)
    return values
