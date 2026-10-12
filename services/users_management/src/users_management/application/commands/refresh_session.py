"""HU003: renovar la sesión con un refresh token (renovación controlada, ADR-009).

En una sola transacción corta, con la fila del token bloqueada:
- token desconocido, vencido o revocado → `invalid_refresh_token`;
- token ya rotado (reutilización) → se revoca toda la familia y se rechaza;
- cuenta deshabilitada → se revoca la familia y se rechaza;
- en otro caso se marca como usado y se emite el siguiente de la familia.
"""

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
)
from users_management.domain.authentication.errors import (
    InvalidRefreshToken,
    RefreshTokenReused,
)
from users_management.domain.authentication.model import RefreshToken

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class RefreshSessionCommand:
    refresh_token: str
    correlation_id: UUID | None = None

    def __repr__(self) -> str:
        return "RefreshSessionCommand(***)"


class RefreshSession:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AuthenticationUnitOfWork],
        token_issuer: AccessTokenIssuer,
        clock: Clock,
        refresh_token_ttl: timedelta,
        new_id: Callable[[], UUID] = uuid4,
        new_secret: Callable[[], str] = new_refresh_secret,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._token_issuer = token_issuer
        self._clock = clock
        self._refresh_token_ttl = refresh_token_ttl
        self._new_id = new_id
        self._new_secret = new_secret

    def __call__(self, command: RefreshSessionCommand) -> SessionResult:
        now = self._clock.now()
        secret = self._new_secret()
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.find_refresh_token_for_update(
                hash_refresh_token(command.refresh_token)
            )
            if current is None:
                self._audit("invalid")
                raise InvalidRefreshToken()
            try:
                current.ensure_usable(now)
            except RefreshTokenReused:
                unit_of_work.revoke_refresh_family(current.family_id, now)
                unit_of_work.commit()
                self._audit("reuse_detected", current)
                raise
            except InvalidRefreshToken:
                self._audit("invalid", current)
                raise
            account = unit_of_work.get_account(current.user_id)
            if account is None or not account.can_authenticate:
                unit_of_work.revoke_refresh_family(current.family_id, now)
                unit_of_work.commit()
                self._audit("account_disabled", current)
                raise InvalidRefreshToken()

            successor = RefreshToken.issue(
                token_id=self._new_id(),
                family_id=current.family_id,
                user_id=current.user_id,
                token_hash=hash_refresh_token(secret),
                now=now,
                ttl=self._refresh_token_ttl,
            )
            unit_of_work.mark_refresh_token_used(current.token_id, now, successor.token_id)
            unit_of_work.add_refresh_token(successor)
            unit_of_work.commit()

        access = self._token_issuer.issue(account.user_id, account.actor_type.value, now)
        self._audit("rotated", current)
        return SessionResult.build(
            account=account,
            access=access,
            refresh_secret=secret,
            refresh_expires_at=successor.expires_at,
            now=now,
        )

    @staticmethod
    def _audit(result: str, token: RefreshToken | None = None) -> None:
        fields = {"result": result}
        if token is not None:
            fields["user_id"] = str(token.user_id)
            fields["family_id"] = str(token.family_id)
        _audit.info("audit.session_refresh", extra={"fields": fields})
