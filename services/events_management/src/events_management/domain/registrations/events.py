"""`usuario.inscrito.v1` (ADR-032, CMP-004).

Tópico `sportapp.eventos.v1`; agregado `event_registration`, clave de orden
`registration_id`, versión 1. El payload no transporta datos del evento, perfil ni pago.
"""

from typing import Any

from events_management.domain.registrations.model import AppliedFee, EventRegistration
from events_management.domain.sport_events.created import utc_instant

EVENT_TYPE = "usuario.inscrito.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "event_registration"


def fee_document(fee: AppliedFee | None) -> dict[str, str] | None:
    return None if fee is None else {"amount": f"{fee.amount:.2f}", "currency": fee.currency}


def registered_payload(registration: EventRegistration) -> dict[str, Any]:
    return {
        "registration_id": str(registration.registration_id),
        "event_id": str(registration.event_id),
        "athlete_id": str(registration.athlete_id),
        "status": registration.status.value,
        "registered_at": utc_instant(registration.registered_at),
        "applied_fee": fee_document(registration.applied_fee),
        "version": registration.version,
    }
