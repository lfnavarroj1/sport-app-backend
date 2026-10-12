"""HU004: cerrar sesión revocando la credencial renovable (decisión del 2026-10-12).

En una sola transacción corta, con la fila del token bloqueada:
- token desconocido → no hay nada que revocar (CA2: respuesta segura, sin revelar nada);
- token de una familia ya revocada → idempotente, no reactiva la sesión (CA2, CA7);
- en otro caso (vigente, ya rotado o vencido) se revoca toda la familia: ningún
  refresh token de esa sesión vuelve a renovar (CA1, CA3).
El access token ya emitido no se invalida: expira por su vigencia corta (≤ 15 min).
La auditoría registra fecha, identidad y resultado, nunca el token (CA4).

La clave de idempotencia es opcional (ADR-026, decisión del 2026-10-12): la cola offline
móvil la repite en cada reintento. No se persiste, porque el propio token ya hace
idempotente la revocación; solo se registra para correlacionar los reintentos.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from users_management.application.commands.sessions import hash_refresh_token
from users_management.application.ports import AuthenticationUnitOfWork, Clock
from users_management.domain.authentication.model import RefreshToken

_audit = logging.getLogger("sportapp.audit")


class RevocationResult(StrEnum):
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RevokeSessionCommand:
    refresh_token: str
    correlation_id: UUID | None = None
    idempotency_key: UUID | None = None

    def __repr__(self) -> str:
        return f"RevokeSessionCommand(idempotency_key={self.idempotency_key})"


class RevokeSession:
    def __init__(
        self, *, unit_of_work: Callable[[], AuthenticationUnitOfWork], clock: Clock
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, command: RevokeSessionCommand) -> RevocationResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            token = unit_of_work.find_refresh_token_for_update(
                hash_refresh_token(command.refresh_token)
            )
            if token is None:
                result = RevocationResult.UNKNOWN
            elif token.revoked_at is not None:
                result = RevocationResult.ALREADY_REVOKED
            else:
                # Una renovación concurrente con otro token de la sesión termina primero; así la
                # revocación (sentencia siguiente) alcanza también al sucesor que haya creado.
                unit_of_work.lock_refresh_family(token.family_id)
                unit_of_work.revoke_refresh_family(token.family_id, now)
                unit_of_work.commit()
                result = RevocationResult.REVOKED
        self._audit(result, token, command.idempotency_key)
        return result

    @staticmethod
    def _audit(
        result: RevocationResult, token: RefreshToken | None, idempotency_key: UUID | None
    ) -> None:
        fields = {"result": result.value}
        if idempotency_key is not None:
            fields["idempotency_key"] = str(idempotency_key)
        if token is not None:
            fields["user_id"] = str(token.user_id)
            fields["family_id"] = str(token.family_id)
        _audit.info("audit.session_revoke", extra={"fields": fields})
