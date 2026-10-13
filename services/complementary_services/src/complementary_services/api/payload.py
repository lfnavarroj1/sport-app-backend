"""Lectura del cuerpo JSON de `POST /v1/services` con los errores de ADR-027 §14.

Esta capa solo verifica la forma del JSON: campos permitidos (`field_not_allowed`,
incluido `provider_id`), obligatorios (`required`) y tipos JSON (`unknown_value`).
Las reglas de la oferta (longitudes, catálogos, tarifa e intervalos) son del dominio.
"""

from typing import Any

from complementary_services.domain.availability.intervals import IntervalInput
from complementary_services.domain.catalog.offer import OfferInput, PriceInput
from complementary_services.domain.validation import Issue, Violation

TOP_LEVEL = ("name", "description", "category", "modality", "price", "availability")
PRICE_FIELDS = ("amount", "currency", "billing_unit")
INTERVAL_FIELDS = ("starts_at", "ends_at", "time_zone", "capacity")


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


def _integer(
    document: dict[str, Any], name: str, path: str, violations: list[Violation]
) -> int | None:
    value = document.get(name)
    if value is None:
        violations.append(Violation(path, Issue.REQUIRED))
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        return None
    return value


def _price(document: dict[str, Any], violations: list[Violation]) -> PriceInput | None:
    value = document.get("price")
    if value is None:
        violations.append(Violation("price", Issue.REQUIRED))
        return None
    if not isinstance(value, dict):
        violations.append(Violation("price", Issue.UNKNOWN_VALUE))
        return None
    violations.extend(_unexpected(value, PRICE_FIELDS, "price."))
    # La tarifa viaja como cadena decimal: un número JSON se rechaza (ADR-027 §2).
    return PriceInput(
        amount=_string(value, "amount", "price.amount", violations),
        currency=_string(value, "currency", "price.currency", violations),
        billing_unit=_string(value, "billing_unit", "price.billing_unit", violations),
    )


def _interval(index: int, value: object, violations: list[Violation]) -> IntervalInput:
    path = f"availability[{index}]"
    if not isinstance(value, dict):
        violations.append(Violation(path, Issue.UNKNOWN_VALUE))
        # Se conserva vacío para no desplazar los índices de los intervalos siguientes.
        return IntervalInput(None, None, None, None)
    violations.extend(_unexpected(value, INTERVAL_FIELDS, f"{path}."))
    return IntervalInput(
        starts_at=_string(value, "starts_at", f"{path}.starts_at", violations),
        ends_at=_string(value, "ends_at", f"{path}.ends_at", violations),
        time_zone=_string(value, "time_zone", f"{path}.time_zone", violations),
        capacity=_integer(value, "capacity", f"{path}.capacity", violations),
    )


def _availability(document: dict[str, Any], violations: list[Violation]) -> list[IntervalInput]:
    value = document.get("availability")
    if value is None:
        return []  # La disponibilidad inicial es opcional.
    if not isinstance(value, list):
        violations.append(Violation("availability", Issue.UNKNOWN_VALUE))
        return []
    return [_interval(index, item, violations) for index, item in enumerate(value)]


def parse_offer(body: object) -> OfferInput:
    """Oferta con los campos de forma válida y las violaciones de transporte encontradas."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return OfferInput(None, None, None, None, None, [], (Violation(None, issue),))
    violations = _unexpected(body, TOP_LEVEL, "")
    name = _string(body, "name", "name", violations)
    description = _string(body, "description", "description", violations)
    category = _string(body, "category", "category", violations)
    modality = _string(body, "modality", "modality", violations)
    price = _price(body, violations)
    availability = _availability(body, violations)
    return OfferInput(
        name=name,
        description=description,
        category=category,
        modality=modality,
        price=price,
        availability=availability,
        transport_violations=tuple(violations),
    )
