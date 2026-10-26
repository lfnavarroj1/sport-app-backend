"""HU031: inicio de la vinculación y callback OAuth de Strava (ADR-042 §5-§6, CA1-CA3).

Inicio (`POST /v1/me/integrations/strava/authorization`): `athlete` con
`integrations.strava.connect` y Strava configurado; guarda un `state` de un solo uso (borra
los vencidos del deportista) y devuelve la URL del consentimiento oficial con
`scope=read,activity:read`. `expires_at` = creación + 10 minutos (R).

Callback (`POST /v1/integrations/strava/callback`, sin token: lo llama el BFF móvil), en este
orden:
1. `state` ausente, mal formado, inexistente, vencido o ya usado, o con un MAC que no
   corresponde al deportista guardado → `INVALID_STATE`.
2. El `state` válido se consume en la misma transacción en que se valida.
3. `error` presente o `code` ausente → `DENIED`.
4. `scope` sin `activity:read` → `DENIED` (R).
5. Intercambio del `code` fuera de toda transacción; cualquier falla → `ERROR`.
6. La cuenta de Strava ya `ACTIVE` para otro deportista → `ERROR` (R).
7. Guarda o reemplaza la vinculación cifrada con `consent_version` → `LINKED`.

Auditoría `audit.strava_link` con `user_id` y `result`; nunca `code`, `state`, tokens ni
respuestas del proveedor.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from third_party_management.application.access import (
    require_athlete,
    require_configured,
    require_strava_capability,
)
from third_party_management.application.errors import AthleteAlreadyLinked, ProviderFailure
from third_party_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    OAuthState,
    StravaContext,
    UnitOfWork,
)
from third_party_management.domain.connections import (
    OAUTH_STATE_TTL,
    ExternalAccount,
    grants_activity_read,
    parse_scopes,
    state_expired,
)

_audit = logging.getLogger("sportapp.audit")
_logger = logging.getLogger("sportapp.integrations")


class CallbackResult(StrEnum):
    LINKED = "LINKED"
    DENIED = "DENIED"
    INVALID_STATE = "INVALID_STATE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AuthorizationLink:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True)
class CallbackInput:
    code: str | None
    state: str | None
    scope: str | None
    error: str | None


class StartAuthorization:
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

    def __call__(self, caller: Caller) -> AuthorizationLink:
        require_athlete(caller)
        require_strava_capability(caller, self._capabilities)
        strava = require_configured(self._strava)
        now = self._clock.now()
        state = strava.states.issue(caller.user_id)
        with self._unit_of_work() as unit_of_work:
            unit_of_work.delete_oauth_states(caller.user_id, now - OAUTH_STATE_TTL)
            unit_of_work.add_oauth_state(OAuthState(state, caller.user_id, now))
            unit_of_work.commit()
        return AuthorizationLink(
            authorization_url=strava.gateway.authorization_url(state),
            expires_at=now + OAUTH_STATE_TTL,
        )


class CompleteCallback:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        strava: StravaContext | None,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._strava = strava
        self._clock = clock

    def __call__(self, callback: CallbackInput) -> CallbackResult:
        strava = require_configured(self._strava)
        user_id = self._consume_state(strava, callback.state)
        if user_id is None:
            result = CallbackResult.INVALID_STATE
        else:
            result = self._link(strava, user_id, callback)
        _audit.info(
            "audit.strava_link",
            extra={
                "fields": {
                    "user_id": str(user_id) if user_id else None,
                    "result": result.value,
                }
            },
        )
        return result

    def _consume_state(self, strava: StravaContext, state: str | None) -> UUID | None:
        """Pasos 1-2: el `state` se borra al leerlo, sea válido o no."""
        if not state or not strava.states.well_formed(state):
            return None
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            stored = unit_of_work.take_oauth_state(state)
            unit_of_work.commit()
        if stored is None or state_expired(stored.created_at, now):
            return None
        if not strava.states.verify(state, stored.user_id):
            return None
        return stored.user_id

    def _link(
        self, strava: StravaContext, user_id: UUID, callback: CallbackInput
    ) -> CallbackResult:
        scopes = parse_scopes(callback.scope)
        if callback.error or not callback.code or not grants_activity_read(scopes):
            return CallbackResult.DENIED
        try:
            grant = strava.gateway.exchange_code(callback.code)
        except ProviderFailure as failure:
            _logger.warning(
                "strava.code_exchange_failed",
                extra={"fields": {"user_id": str(user_id), "failure": failure.kind.value}},
            )
            return CallbackResult.ERROR
        sealed = strava.sealer.seal(user_id, grant.tokens)
        now = self._clock.now()
        try:
            with self._unit_of_work() as unit_of_work:
                owner = unit_of_work.active_owner_of(grant.external_athlete_id)
                if owner is not None and owner != user_id:
                    return CallbackResult.ERROR
                current = unit_of_work.get_account(user_id, for_update=True)
                unit_of_work.save_account(
                    ExternalAccount.link(
                        current=current,
                        user_id=user_id,
                        external_athlete_id=grant.external_athlete_id,
                        tokens=sealed,
                        scopes=scopes,
                        consent_version=strava.consent_version,
                        now=now,
                    )
                )
                unit_of_work.commit()
        except AthleteAlreadyLinked:
            return CallbackResult.ERROR
        return CallbackResult.LINKED
