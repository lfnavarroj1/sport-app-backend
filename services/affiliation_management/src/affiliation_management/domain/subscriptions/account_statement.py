"""HU007 (ADR-036): reglas del estado de cuenta, derivado de la suscripción, sus compromisos
informativos y el historial de movimientos. SportApp **no registra pagos**: no hay saldo.

- `organizer` y `service_provider` → `NOT_APPLICABLE` con listas vacías (CA2).
- Deportista con suscripción activa → `ACTIVE`.
- Deportista sin ninguna suscripción (alta `FREE` en curso) → `PROVISIONING` con listas
  vacías (CA2), igual que `GET /v1/me/subscription` (decisión del responsable, 2026-10-23).
- Deportista con suscripciones anteriores pero ninguna activa → `NONE`: la misma lectura
  de «sin suscripción activa» del contrato interno de capacidades (ADR-024); conserva sus
  compromisos y movimientos. El cambio de plan nunca deja ese estado (cierre y alta en una
  transacción), así que solo aparece ante datos anómalos.

Compromisos listados: los de la suscripción vigente y los anteriores **no cerrados**, es decir,
cuyo periodo aún no termina (`period_end > now`), en cualquier estado (`PENDING_EXTERNAL` o
`WAIVED`). Los demás siguen visibles como movimientos.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from affiliation_management.domain.subscriptions.change import BillingCommitment
from affiliation_management.domain.subscriptions.model import ATHLETE

PAYMENTS_NOT_INTEGRATED = "NOT_INTEGRATED"


class StatementStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PROVISIONING = "PROVISIONING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NONE = "NONE"


def statement_status(
    actor_type: str, *, has_active_subscription: bool, has_any_subscription: bool
) -> StatementStatus:
    if actor_type != ATHLETE:
        return StatementStatus.NOT_APPLICABLE
    if has_active_subscription:
        return StatementStatus.ACTIVE
    if has_any_subscription:
        return StatementStatus.NONE
    return StatementStatus.PROVISIONING


def is_listed(
    commitment: BillingCommitment, *, current_subscription_id: UUID | None, now: datetime
) -> bool:
    """El compromiso es de la suscripción vigente o su periodo todavía no terminó."""
    return commitment.subscription_id == current_subscription_id or commitment.period_end > now


def listed_commitments(
    commitments: list[BillingCommitment],
    *,
    current_subscription_id: UUID | None,
    now: datetime,
) -> list[BillingCommitment]:
    """Compromisos visibles, del periodo más reciente al más antiguo."""
    visible = [
        commitment
        for commitment in commitments
        if is_listed(commitment, current_subscription_id=current_subscription_id, now=now)
    ]
    return sorted(
        visible,
        key=lambda commitment: (commitment.period_start, commitment.commitment_id.int),
        reverse=True,
    )
