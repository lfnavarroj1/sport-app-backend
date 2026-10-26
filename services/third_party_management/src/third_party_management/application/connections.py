"""HU031: estado de la vinculación y desvinculación (ADR-042 §7, CA9).

Estado (`GET /v1/me/integrations/strava`): `NOT_LINKED` sin cuenta; si no, el estado, el
atleta de Strava, `linked_at`, los scopes aceptados y la última corrida.

Desvinculación (`DELETE /v1/me/integrations/strava`, `204` siempre, R):
1. Si hay tokens y Strava está configurado, `deauthorize` en el proveedor fuera de toda
   transacción (mejor esfuerzo con un reintento; su falla no impide desvincular). Sin
   configuración se desvincula igual y se registra (R).
2. En una transacción con la cuenta bloqueada: tokens borrados, `REVOKED` con la versión
   siguiente, `revoked_at`; las corridas `QUEUED`/`RUNNING` → `FAILED`
   `integration_revoked`; los `state` pendientes del deportista se borran. Las actividades
   importadas se conservan.

Auditoría `audit.strava_unlink` con `user_id`, `result` y el resultado de la revocación en
el proveedor.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from third_party_management.application.access import require_strava_capability
from third_party_management.application.errors import ProviderFailure
from third_party_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    StravaContext,
    UnitOfWork,
)
from third_party_management.domain.connections import ExternalAccount
from third_party_management.domain.synchronization import SyncRun

_audit = logging.getLogger("sportapp.audit")
_logger = logging.getLogger("sportapp.integrations")


@dataclass(frozen=True)
class IntegrationStatus:
    account: ExternalAccount | None
    last_sync: SyncRun | None


class GetIntegrationStatus:
    def __init__(
        self, *, unit_of_work: Callable[[], UnitOfWork], capabilities: CapabilityChecker
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities

    def __call__(self, caller: Caller) -> IntegrationStatus:
        require_strava_capability(caller, self._capabilities)
        with self._unit_of_work() as unit_of_work:
            return IntegrationStatus(
                account=unit_of_work.get_account(caller.user_id),
                last_sync=unit_of_work.latest_run(caller.user_id),
            )


class UnlinkStrava:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        capabilities: CapabilityChecker,
        strava: StravaContext | None,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._strava = strava
        self._clock = clock

    def __call__(self, caller: Caller) -> None:
        require_strava_capability(caller, self._capabilities)
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.get_account(caller.user_id)
        provider = self._deauthorize(current)
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            account = unit_of_work.get_account(caller.user_id, for_update=True)
            if account is None:
                result = "not_linked"
            else:
                revoked = account.revoke(now)
                result = "revoked" if revoked is not account else "already_revoked"
                if revoked is not account:
                    unit_of_work.save_account(revoked)
                for run in unit_of_work.pending_runs(caller.user_id):
                    unit_of_work.save_run(run.cancel(now))
            unit_of_work.delete_oauth_states(caller.user_id, None)
            unit_of_work.commit()
        _audit.info(
            "audit.strava_unlink",
            extra={
                "fields": {
                    "user_id": str(caller.user_id),
                    "result": result,
                    "provider_revocation": provider,
                }
            },
        )

    def _deauthorize(self, account: ExternalAccount | None) -> str:
        if account is None or account.tokens is None:
            return "skipped"
        if self._strava is None:
            _logger.warning(
                "strava.deauthorize_skipped",
                extra={"fields": {"user_id": str(account.user_id), "reason": "not_configured"}},
            )
            return "skipped"
        try:
            tokens = self._strava.sealer.open(account.user_id, account.tokens)
            self._strava.gateway.deauthorize(tokens.access_token)
        except ProviderFailure as failure:
            _logger.warning(
                "strava.deauthorize_failed",
                extra={"fields": {"user_id": str(account.user_id), "failure": failure.kind.value}},
            )
            return "failed"
        except ValueError:
            # El token no se pudo descifrar (clave rotada): se desvincula igual.
            _logger.warning(
                "strava.deauthorize_failed",
                extra={"fields": {"user_id": str(account.user_id), "failure": "undecryptable"}},
            )
            return "failed"
        return "ok"
