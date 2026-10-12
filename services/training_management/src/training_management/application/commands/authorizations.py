"""HU010: el deportista autoriza o revoca a un profesional.

- Autorizar exige la capacidad `training.plan.manage` (se evalúa antes de abrir la
  transacción). Es idempotente por naturaleza: si ya está activa se devuelve la
  existente; si estaba revocada se reactiva.
- Revocar solo exige ser deportista: retirar un consentimiento nunca depende del
  plan contratado. Revocar algo inexistente o ya revocado no tiene efecto.

HU011: cada cambio real de estado deja en la outbox, en la misma transacción,
`autorizacion_entrenamiento.otorgada.v1` o `.revocada.v1` para proyectar la
autorización a la base de lectura. Sin cambio no se emite nada.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from training_management.application.access import require_athlete, require_plan_capability
from training_management.application.errors import AuthorizationConflict
from training_management.application.ports import (
    AuthorizationUnitOfWork,
    Caller,
    CapabilityChecker,
    Clock,
    OutboxMessage,
)
from training_management.domain.authorizations.events import (
    AGGREGATE_TYPE,
    SCHEMA_VERSION,
    authorization_aggregate_id,
    authorization_event_type,
    authorization_payload,
)
from training_management.domain.authorizations.model import TrainingAuthorization

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class GrantResult:
    authorization: TrainingAuthorization
    created: bool


def authorization_changed(
    authorization: TrainingAuthorization,
    *,
    event_id: UUID,
    now: datetime,
    correlation_id: UUID | None,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id,
        event_type=authorization_event_type(authorization),
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=authorization_aggregate_id(
            authorization.athlete_id, authorization.professional_id
        ),
        aggregate_version=authorization.version,
        occurred_at=now,
        correlation_id=correlation_id,
        causation_id=None,
        payload=authorization_payload(authorization),
    )


class GrantAuthorization:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AuthorizationUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock
        self._new_id = new_id

    def __call__(
        self, caller: Caller, professional_id: UUID, correlation_id: UUID | None = None
    ) -> GrantResult:
        require_plan_capability(caller, self._capabilities)
        try:
            result = self._grant(caller.user_id, professional_id, correlation_id)
        except AuthorizationConflict:
            # Una solicitud concurrente la creó primero: queda activa y se devuelve.
            result = self._grant(caller.user_id, professional_id, correlation_id)
        _audit.info(
            "audit.training_authorization",
            extra={
                "fields": {
                    "action": "granted" if result.created else "already_active",
                    "athlete_id": str(caller.user_id),
                    "professional_id": str(professional_id),
                }
            },
        )
        return result

    def _grant(
        self, athlete_id: UUID, professional_id: UUID, correlation_id: UUID | None
    ) -> GrantResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.get_for_update(athlete_id, professional_id)
            if current is not None and current.is_active:
                return GrantResult(current, created=False)
            if current is None:
                granted = TrainingAuthorization.grant(
                    athlete_id=athlete_id, professional_id=professional_id, now=now
                )
                unit_of_work.add(granted)
            else:
                granted = current.reactivate(now)
                unit_of_work.update(granted)
            unit_of_work.add_outbox(
                authorization_changed(
                    granted, event_id=self._new_id(), now=now, correlation_id=correlation_id
                )
            )
            unit_of_work.commit()
        return GrantResult(granted, created=True)


class RevokeAuthorization:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AuthorizationUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    def __call__(
        self, caller: Caller, professional_id: UUID, correlation_id: UUID | None = None
    ) -> None:
        require_athlete(caller)
        revoked = False
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.get_for_update(caller.user_id, professional_id)
            if current is not None and current.is_active:
                now = self._clock.now()
                revocation = current.revoke(now)
                unit_of_work.update(revocation)
                unit_of_work.add_outbox(
                    authorization_changed(
                        revocation, event_id=self._new_id(), now=now, correlation_id=correlation_id
                    )
                )
                unit_of_work.commit()
                revoked = True
        _audit.info(
            "audit.training_authorization",
            extra={
                "fields": {
                    "action": "revoked" if revoked else "not_active",
                    "athlete_id": str(caller.user_id),
                    "professional_id": str(professional_id),
                }
            },
        )
