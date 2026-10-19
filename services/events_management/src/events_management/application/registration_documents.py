"""Representación de la inscripción de HU041 en respuestas y en el resultado idempotente.

Instantes en UTC con sufijo `Z`; `applied_fee` es informativo (`null` sin tarifa de HU025).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from events_management.application.ports import EventSummary, RegistrationView
from events_management.domain.registrations.events import fee_document
from events_management.domain.registrations.model import (
    AppliedFee,
    EventRegistration,
    RegistrationStatus,
)
from events_management.domain.sport_events.created import utc_instant


def summary_document(event: EventSummary) -> dict[str, Any]:
    return {
        "name": event.name,
        "starts_at": utc_instant(event.starts_at),
        "ends_at": utc_instant(event.ends_at),
        "time_zone": event.time_zone,
        "location": {
            "name": event.location_name,
            "city": event.location_city,
            "country_code": event.location_country_code,
        },
    }


def registration_document(view: RegistrationView) -> dict[str, Any]:
    registration = view.registration
    return {
        "registration_id": str(registration.registration_id),
        "event_id": str(registration.event_id),
        "status": registration.status.value,
        "registered_at": utc_instant(registration.registered_at),
        "applied_fee": fee_document(registration.applied_fee),
        "version": registration.version,
        "event": summary_document(view.event),
    }


def registration_from_document(document: dict[str, Any], athlete_id: UUID) -> RegistrationView:
    """Reconstruye el resultado guardado; `athlete_id` no viaja en el documento público."""
    fee = document.get("applied_fee")
    registered_at = datetime.fromisoformat(document["registered_at"])
    event = document["event"]
    return RegistrationView(
        registration=EventRegistration(
            registration_id=UUID(document["registration_id"]),
            event_id=UUID(document["event_id"]),
            athlete_id=athlete_id,
            status=RegistrationStatus(document["status"]),
            registered_at=registered_at,
            applied_fee=AppliedFee(Decimal(fee["amount"]), fee["currency"]) if fee else None,
            version=int(document["version"]),
            created_at=registered_at,
            updated_at=registered_at,
        ),
        event=EventSummary(
            name=event["name"],
            starts_at=datetime.fromisoformat(event["starts_at"]),
            ends_at=datetime.fromisoformat(event["ends_at"]),
            time_zone=event["time_zone"],
            location_name=event["location"]["name"],
            location_city=event["location"]["city"],
            location_country_code=event["location"]["country_code"],
        ),
    )
