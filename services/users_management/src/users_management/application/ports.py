"""Puertos que la aplicación necesita; la infraestructura los implementa."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from users_management.domain.account.model import AccountDetails, ActorType, UserAccount
from users_management.domain.authentication.model import FailureWindow, RefreshToken
from users_management.domain.profile.events import ProfileSection


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...


class PasswordVerifier(Protocol):
    def verify(self, password_hash: str | None, password: str) -> bool:
        """Sin hash (correo inexistente) verifica contra uno ficticio y devuelve `False`,
        para que el tiempo de respuesta no revele si la cuenta existe."""
        ...


@dataclass(frozen=True)
class IssuedAccessToken:
    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"IssuedAccessToken(expires_at={self.expires_at.isoformat()})"


class AccessTokenIssuer(Protocol):
    def issue(self, user_id: UUID, actor_type: str, now: datetime) -> IssuedAccessToken: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


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


class RegistrationUnitOfWork(Protocol):
    """Transacción local del registro: cuenta, outbox e idempotencia juntas."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None: ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def email_exists(self, email: str) -> bool: ...

    def add_account(self, account: UserAccount) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada.

        Traduce violaciones de unicidad a `EmailAlreadyRegistered` o
        `IdempotencyKeyConflict` para que la aplicación no conozca SQL.
        """
        ...


class AuthenticationUnitOfWork(Protocol):
    """Transacción local corta del inicio de sesión (HU003); nunca abarca el hash ni la firma."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_account_by_email(self, email: str) -> UserAccount | None: ...

    def get_account(self, user_id: UUID) -> UserAccount | None: ...

    def find_failure_window(self, throttle_key: str) -> FailureWindow | None: ...

    def record_failure(self, throttle_key: str, now: datetime, window: timedelta) -> FailureWindow:
        """Suma un fallo de forma atómica aunque haya intentos concurrentes."""
        ...

    def clear_failures(self, throttle_key: str) -> None: ...

    def add_refresh_token(self, token: RefreshToken) -> None: ...

    def find_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        """Bloquea la fila hasta el fin de la transacción: dos renovaciones con el mismo
        token se serializan y la segunda lo encuentra ya usado."""
        ...

    def mark_refresh_token_used(
        self, token_id: UUID, used_at: datetime, replaced_by: UUID
    ) -> None: ...

    def lock_refresh_family(self, family_id: UUID) -> None:
        """Bloquea los tokens de la sesión hasta el fin de la transacción (HU004): espera a una
        renovación en curso para que la revocación posterior incluya a su sucesor."""
        ...

    def revoke_refresh_family(self, family_id: UUID, revoked_at: datetime) -> None: ...

    def commit(self) -> None: ...


@dataclass(frozen=True)
class UserReference:
    """Identidad mínima resuelta por correo (HU010); sin nombre, correo ni credenciales."""

    user_id: UUID
    actor_type: ActorType


class UserDirectory(Protocol):
    """Directorio de identidad en la base de lectura."""

    def find_active_by_email(self, email: str) -> UserReference | None: ...


class NutritionRestrictionsReader(Protocol):
    """Restricciones alimenticias proyectadas de una cuenta activa (ADR-035, HU009).

    `None` si no hay una cuenta activa con ese identificador; `version = None` (listas vacías)
    si la cuenta no registró hábitos alimenticios. `ReadModelUnavailable` si la base de
    lectura no responde.
    """

    def find(self, user_id: UUID) -> "NutritionRestrictions | None": ...


@dataclass(frozen=True)
class NutritionRestrictions:
    """Contrato interno de ADR-035: datos de salud que solo recibe `training-management`."""

    restrictions: tuple[str, ...]
    allergies: tuple[str, ...]
    diet_pattern: str | None
    version: int | None


class AthleteContextReader(Protocol):
    """Contexto del deportista proyectado en `athlete_profile_view` (ADR-041, HU026).

    `None` si no hay una cuenta activa con ese identificador; los campos vacíos o `None` si
    la cuenta aún no registró la sección correspondiente (HU005 o HU006).
    `ReadModelUnavailable` si la base de lectura no responde.
    """

    def find(self, user_id: UUID) -> "AthleteContext | None": ...


@dataclass(frozen=True)
class AthleteContext:
    """Contrato interno de ADR-041: deportes y objetivo (HU006), ciudad, país y fecha de
    nacimiento (HU005). Datos personales: nunca en trazas ni logs."""

    sports: tuple[str, ...]
    primary_goal: str | None
    city: str | None
    country_code: str | None
    birth_date: str | None

    def __repr__(self) -> str:
        return f"AthleteContext(sports={len(self.sports)})"

    @property
    def is_empty(self) -> bool:
        return not self.sports and self.city is None and self.birth_date is None


@dataclass(frozen=True)
class UserDisplay:
    """Nombre y correo de una cuenta activa (HU011, CA5). Datos personales: solo los recibe el
    BFF para el profesional autorizado; nunca se registran."""

    user_id: UUID
    full_name: str
    email: str

    def __repr__(self) -> str:
        return f"UserDisplay(user_id={self.user_id})"


class UserDisplayDirectory(Protocol):
    def find_active_by_ids(self, user_ids: tuple[UUID, ...]) -> list[UserDisplay]:
        """Cuentas activas con nombre proyectado entre `user_ids`; las demás se omiten."""
        ...


@dataclass(frozen=True)
class StoredProfileSection:
    """Sección vigente del perfil (HU005). `document` es el cuerpo de la respuesta del
    comando, incluido `user_id`; es también el payload del evento de la sección."""

    section: ProfileSection
    user_id: UUID
    version: int
    document: dict[str, Any]

    def __repr__(self) -> str:
        # Los valores son datos sensibles (demográficos y de salud): nunca en trazas.
        return f"StoredProfileSection(section={self.section}, version={self.version})"


class ProfileUnitOfWork(Protocol):
    """Transacción local de un comando de perfil: sección, outbox e idempotencia juntas."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        """Sin *autoflush*: una consulta no debe adelantar el conflicto al `commit`."""
        ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def lock_section(self, section: ProfileSection, user_id: UUID) -> StoredProfileSection | None:
        """Bloquea la fila de la sección (`FOR UPDATE`) hasta el fin de la transacción."""
        ...

    def save_section(self, stored: StoredProfileSection, *, created: bool) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada. Traduce la unicidad de la clave a `IdempotencyKeyConflict`
        y la de la sección (alta concurrente) a `SectionAlreadyExists`."""
        ...


@dataclass(frozen=True)
class AthleteProfileView:
    """Fila de `athlete_profile_view`. Cada sección es el documento del comando sin
    `user_id`, o `None` si todavía no se ha proyectado."""

    user_id: UUID
    demographic: dict[str, Any] | None
    physiological: dict[str, Any] | None
    sport_habits: dict[str, Any] | None
    nutrition_habits: dict[str, Any] | None
    demographic_version: int | None
    physiological_version: int | None
    sport_habits_version: int | None
    nutrition_habits_version: int | None
    projected_at: datetime | None

    def __repr__(self) -> str:
        return f"AthleteProfileView(user_id={self.user_id})"


class AthleteProfileReader(Protocol):
    """Lectura del perfil propio en la base de lectura (HU005)."""

    def get(self, user_id: UUID) -> AthleteProfileView | None:
        """`None` si no hay fila; `ReadModelUnavailable` si la base de lectura no responde."""
        ...


class AccountUnitOfWork(Protocol):
    """Transacción local de `PUT /v1/me/account` (HU002): cuenta, outbox e idempotencia."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        """Sin *autoflush*: una consulta no debe adelantar el conflicto al `commit`."""
        ...

    def delete_idempotency(self, key: UUID) -> None: ...

    def lock_account(self, user_id: UUID) -> AccountDetails | None:
        """Bloquea la fila de `user_account` (`FOR UPDATE`) hasta el fin de la transacción."""
        ...

    def email_taken(self, email: str, user_id: UUID) -> bool:
        """`True` si otra cuenta ya usa ese correo normalizado."""
        ...

    def save_account(self, account: AccountDetails) -> None: ...

    def add_outbox(self, message: OutboxMessage) -> None: ...

    def add_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None:
        """Confirma todo o nada. Traduce la unicidad del correo a `EmailAlreadyRegistered` y
        la de la clave a `IdempotencyKeyConflict`."""
        ...


@dataclass(frozen=True)
class AccountView:
    """Cuenta proyectada en `user_directory` (HU002). Datos personales: nunca en trazas."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: str
    status: str
    version: int
    projected_at: datetime

    def __repr__(self) -> str:
        return f"AccountView(user_id={self.user_id}, version={self.version})"


class AccountReader(Protocol):
    def get(self, user_id: UUID) -> AccountView | None:
        """`None` si la cuenta aún no está proyectada (o sin nombre); `ReadModelUnavailable` si
        la base de lectura no responde."""
        ...
