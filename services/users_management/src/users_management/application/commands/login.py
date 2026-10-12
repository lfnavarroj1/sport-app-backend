"""HU003: iniciar sesión.

Secuencia:
1. Normalizar el correo y calcular la clave de control de abuso (correo + IP).
2. En una transacción corta de lectura: si la combinación está limitada, rechazar
   con el tiempo de espera (CA4); si no, buscar la cuenta.
3. Verificar la contraseña con Argon2id fuera de toda transacción. Un correo
   inexistente se verifica contra un hash ficticio (CA2, sin enumeración).
4. Si falla: sumar el intento de forma atómica y responder con el error genérico.
   Si la cuenta está deshabilitada: mismo error genérico y auditoría (CA3).
5. Si es válida: en una transacción, reiniciar el contador y guardar el hash del
   refresh token; luego firmar el access token (CA1).
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from users_management.application.commands.sessions import (
    SessionResult,
    hash_refresh_token,
    new_refresh_secret,
)
from users_management.application.ports import (
    AccessTokenIssuer,
    AuthenticationUnitOfWork,
    Clock,
    PasswordVerifier,
)
from users_management.domain.account.model import UserAccount
from users_management.domain.authentication.errors import (
    InvalidCredentials,
    TooManyLoginAttempts,
)
from users_management.domain.authentication.model import LoginThrottlePolicy, RefreshToken

_audit = logging.getLogger("sportapp.audit")


def normalize_login_email(email: str) -> str:
    """Misma normalización que el registro, sin validar formato: un correo mal escrito
    recibe el error genérico, no uno que revele qué está mal."""
    return email.strip().lower()


def throttle_key(email: str, client_ip: str) -> str:
    """Se guarda solo el hash: la tabla no conserva correos que quizá no existen."""
    return hashlib.sha256(f"{email}\x00{client_ip}".encode()).hexdigest()


@dataclass(frozen=True)
class LoginCommand:
    email: str
    password: str
    client_ip: str
    correlation_id: UUID | None = None

    def __repr__(self) -> str:
        # Nunca exponer correo ni contraseña en trazas o logs.
        return "LoginCommand(***)"


class Login:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AuthenticationUnitOfWork],
        password_verifier: PasswordVerifier,
        token_issuer: AccessTokenIssuer,
        clock: Clock,
        throttle: LoginThrottlePolicy,
        refresh_token_ttl: timedelta,
        new_id: Callable[[], UUID] = uuid4,
        new_secret: Callable[[], str] = new_refresh_secret,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._password_verifier = password_verifier
        self._token_issuer = token_issuer
        self._clock = clock
        self._throttle = throttle
        self._refresh_token_ttl = refresh_token_ttl
        self._new_id = new_id
        self._new_secret = new_secret

    def __call__(self, command: LoginCommand) -> SessionResult:
        email = normalize_login_email(command.email)
        key = throttle_key(email, command.client_ip)

        with self._unit_of_work() as unit_of_work:
            retry_after = self._throttle.retry_after(
                unit_of_work.find_failure_window(key), self._clock.now()
            )
            account = None if retry_after else unit_of_work.find_account_by_email(email)
        if retry_after:
            self._audit("throttled")
            raise TooManyLoginAttempts(retry_after)

        # Se verifica siempre, también sin cuenta, para igualar el tiempo de respuesta.
        password_hash = account.password_hash if account else None
        password_matches = self._password_verifier.verify(password_hash, command.password)
        if account is None or not password_matches:
            with self._unit_of_work() as unit_of_work:
                unit_of_work.record_failure(key, self._clock.now(), self._throttle.window)
                unit_of_work.commit()
            self._audit("invalid_credentials", account)
            raise InvalidCredentials()

        if not account.can_authenticate:
            self._audit("account_disabled", account)
            raise InvalidCredentials()

        result = self._open_session(account, key)
        self._audit("success", account)
        return result

    def _open_session(self, account: UserAccount, key: str) -> SessionResult:
        now = self._clock.now()
        secret = self._new_secret()
        refresh = RefreshToken.issue(
            token_id=self._new_id(),
            family_id=self._new_id(),
            user_id=account.user_id,
            token_hash=hash_refresh_token(secret),
            now=now,
            ttl=self._refresh_token_ttl,
        )
        with self._unit_of_work() as unit_of_work:
            unit_of_work.clear_failures(key)
            unit_of_work.add_refresh_token(refresh)
            unit_of_work.commit()
        access = self._token_issuer.issue(account.user_id, account.actor_type.value, now)
        return SessionResult.build(
            account=account,
            access=access,
            refresh_secret=secret,
            refresh_expires_at=refresh.expires_at,
            now=now,
        )

    @staticmethod
    def _audit(result: str, account: UserAccount | None = None) -> None:
        fields = {"result": result}
        if account is not None:
            fields["user_id"] = str(account.user_id)
        _audit.info("audit.user_login", extra={"fields": fields})
