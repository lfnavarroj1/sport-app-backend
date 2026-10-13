"""Lectura del cuerpo JSON de `POST /v1/events` con los errores de ADR-028 §9.

Esta capa solo verifica la forma del JSON: campos permitidos (`field_not_allowed`,
incluidos `organizer_id`, `event_id`, `ends_at`, `status`, `version` y las marcas de
tiempo), obligatorios (`required`) y tipos JSON (`unknown_value`). Las reglas del evento
(longitudes, país, fecha futura, zona, duración y capacidad) son del dominio.
"""

from typing import Any

from events_management.domain.sport_events.model import EventInput, LocationInput
from events_management.domain.validation import Issue, Violation

TOP_LEVEL = (
    "name",
    "description",
    "discipline",
    "location",
    "starts_at",
    "time_zone",
    "duration_minutes",
    "capacity",
)
LOCATION_FIELDS = ("name", "address", "city", "country_code")


def _unexpected(document: dict[str, Any], allowed: tuple[str, ...], prefix: str) -> list[Violation]:
    return [
        Violation(f"{prefix}{name}", Issue.FIELD_NOT_ALLOWED)
        for name in document
        if name not in allowed
    ]


def _string(
    document: dict[str, Any], name: str, path: str, violations: list[Violation]
) -> str | None:
    value = document.get(name)
    if value is None:
        violations.append(Violation(path, Issue.REQUIRED))
        return None
    if not isinstance(value, str):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    return value


def _integer(document: dict[str, Any], name: str, violations: list[Violation]) -> int | None:
    value = document.get(name)
    if value is None:
        violations.append(Violation(name, Issue.REQUIRED))
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None
    return value


def _location(document: dict[str, Any], violations: list[Violation]) -> LocationInput | None:
    value = document.get("location")
    if value is None:
        violations.append(Violation("location", Issue.REQUIRED))
        return None
    if not isinstance(value, dict):
        violations.append(Violation("location", Issue.UNKNOWN_VALUE))
        return None
    violations.extend(_unexpected(value, LOCATION_FIELDS, "location."))
    return LocationInput(
        name=_string(value, "name", "location.name", violations),
        address=_string(value, "address", "location.address", violations),
        city=_string(value, "city", "location.city", violations),
        country_code=_string(value, "country_code", "location.country_code", violations),
    )


def parse_event(body: object) -> EventInput:
    """Evento con los campos de forma válida y las violaciones de transporte encontradas."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return EventInput(None, None, None, None, None, None, None, None, (Violation(None, issue),))
    violations = _unexpected(body, TOP_LEVEL, "")
    return EventInput(
        name=_string(body, "name", "name", violations),
        description=_string(body, "description", "description", violations),
        discipline=_string(body, "discipline", "discipline", violations),
        location=_location(body, violations),
        starts_at=_string(body, "starts_at", "starts_at", violations),
        time_zone=_string(body, "time_zone", "time_zone", violations),
        duration_minutes=_integer(body, "duration_minutes", violations),
        capacity=_integer(body, "capacity", violations),
        transport_violations=tuple(violations),
    )
