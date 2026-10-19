"""Oferta de servicio en borrador (HU015, ADR-027).

- Nombre de 3 a 120 caracteres y descripción de 20 a 2.000, tras recortar los
  espacios exteriores (se persisten recortados).
- Categoría, modalidad y unidad de cobro de catálogos cerrados.
- Tarifa en `COP` como cadena decimal de `0.00` a `999999999.99`, máximo dos decimales;
  es un precio público de referencia, no un cobro.
- Disponibilidad inicial opcional por intervalos explícitos.
- Nace en `DRAFT`, versión 1, asociada al profesional autenticado.
- HU019: se publica (`DRAFT` → `PUBLISHED`) si la oferta almacenada sigue cumpliendo estas
  reglas y tiene por lo menos un intervalo con inicio futuro; los vencidos se ignoran.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from complementary_services.domain.availability.intervals import (
    AvailabilityInterval,
    IntervalInput,
    validate_availability,
)
from complementary_services.domain.validation import InvalidServiceOffer, Issue, Violation

NAME_MIN, NAME_MAX = 3, 120
DESCRIPTION_MIN, DESCRIPTION_MAX = 20, 2000
AMOUNT_MIN = Decimal("0.00")
AMOUNT_MAX = Decimal("999999999.99")
MAX_DECIMALS = 2


class Category(StrEnum):
    SPORTS_MEDICINE = "SPORTS_MEDICINE"
    COACHING = "COACHING"
    NUTRITION = "NUTRITION"
    TRANSPORT = "TRANSPORT"
    ACCOMPANIMENT = "ACCOMPANIMENT"


class Modality(StrEnum):
    IN_PERSON = "IN_PERSON"
    VIRTUAL = "VIRTUAL"
    HYBRID = "HYBRID"


class Currency(StrEnum):
    COP = "COP"


class BillingUnit(StrEnum):
    SERVICE = "SERVICE"
    SESSION = "SESSION"
    HOUR = "HOUR"
    DAY = "DAY"


class ServiceStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True)
class PriceInput:
    amount: str | None
    currency: str | None
    billing_unit: str | None


@dataclass(frozen=True)
class OfferInput:
    """Oferta tal como llega; `None` en un campo que faltaba o tenía otro tipo JSON
    (esa violación ya la informó la capa de transporte)."""

    name: str | None
    description: str | None
    category: str | None
    modality: str | None
    price: PriceInput | None
    availability: list[IntervalInput]
    transport_violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: Currency
    billing_unit: BillingUnit


@dataclass(frozen=True)
class ValidOffer:
    name: str
    description: str
    category: Category
    modality: Modality
    price: Price
    availability: tuple[AvailabilityInterval, ...]


def _text(
    value: str | None, field: str, minimum: int, maximum: int
) -> tuple[str | None, list[Violation]]:
    if value is None:
        return None, []
    trimmed = value.strip()
    if not minimum <= len(trimmed) <= maximum:
        return None, [Violation(field, Issue.INVALID_LENGTH)]
    return trimmed, []


def _choice[E: StrEnum](
    value: str | None, field: str, catalog: type[E]
) -> tuple[E | None, list[Violation]]:
    if value is None:
        return None, []
    try:
        return catalog(value), []
    except ValueError:
        return None, [Violation(field, Issue.UNKNOWN_VALUE)]


def _amount(value: str | None) -> tuple[Decimal | None, list[Violation]]:
    if value is None:
        return None, []
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None, [Violation("price.amount", Issue.UNKNOWN_VALUE)]
    if not amount.is_finite():
        return None, [Violation("price.amount", Issue.UNKNOWN_VALUE)]
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -MAX_DECIMALS:
        return None, [Violation("price.amount", Issue.TOO_MANY_DECIMALS)]
    if not AMOUNT_MIN <= amount <= AMOUNT_MAX:
        return None, [Violation("price.amount", Issue.OUT_OF_RANGE)]
    return amount.quantize(Decimal("0.01")), []


def _price(value: PriceInput | None) -> tuple[Price | None, list[Violation]]:
    if value is None:
        return None, []
    amount, violations = _amount(value.amount)
    currency: Currency | None = None
    if value.currency is not None:
        if value.currency == Currency.COP.value:
            currency = Currency.COP
        else:
            violations.append(Violation("price.currency", Issue.UNSUPPORTED_CURRENCY))
    unit, unit_problems = _choice(value.billing_unit, "price.billing_unit", BillingUnit)
    violations.extend(unit_problems)
    if violations or amount is None or currency is None or unit is None:
        return None, violations
    return Price(amount=amount, currency=currency, billing_unit=unit), []


def validate_offer(offer: OfferInput, now: datetime) -> ValidOffer:
    """Oferta normalizada, o `InvalidServiceOffer` con todas las reglas incumplidas.

    Los campos `None` ya fueron informados por la capa de transporte; si hay alguno,
    se informan igualmente las demás reglas, pero no se devuelve una oferta.
    """
    violations = list(offer.transport_violations)
    name, problems = _text(offer.name, "name", NAME_MIN, NAME_MAX)
    violations.extend(problems)
    description, problems = _text(
        offer.description, "description", DESCRIPTION_MIN, DESCRIPTION_MAX
    )
    violations.extend(problems)
    category, problems = _choice(offer.category, "category", Category)
    violations.extend(problems)
    modality, problems = _choice(offer.modality, "modality", Modality)
    violations.extend(problems)
    price, problems = _price(offer.price)
    violations.extend(problems)
    availability, problems = validate_availability(offer.availability, now)
    violations.extend(problems)
    if violations:
        raise InvalidServiceOffer(violations)
    if name is None or description is None or category is None or modality is None or price is None:
        # Faltaba algún campo: la capa de transporte ya lo informó como `required`.
        raise InvalidServiceOffer([])
    return ValidOffer(
        name=name,
        description=description,
        category=category,
        modality=modality,
        price=price,
        availability=availability,
    )


@dataclass(frozen=True)
class ServiceOffer:
    service_id: UUID
    provider_id: UUID
    name: str
    description: str
    category: Category
    modality: Modality
    price: Price
    availability: tuple[AvailabilityInterval, ...]
    status: ServiceStatus
    version: int
    created_at: datetime
    updated_at: datetime
    # HU019: instante de la publicación; `None` mientras es borrador.
    published_at: datetime | None = None

    @classmethod
    def draft(
        cls, *, service_id: UUID, provider_id: UUID, offer: ValidOffer, now: datetime
    ) -> "ServiceOffer":
        return cls(
            service_id=service_id,
            provider_id=provider_id,
            name=offer.name,
            description=offer.description,
            category=offer.category,
            modality=offer.modality,
            price=offer.price,
            availability=offer.availability,
            status=ServiceStatus.DRAFT,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def publish(self, now: datetime) -> "ServiceOffer":
        """HU019: `DRAFT` → `PUBLISHED`; sube la versión una vez y fija `published_at`.

        Solo desde `DRAFT`: un servicio ya publicado lo resuelve el caso de uso sin
        transición. `InvalidServiceOffer` si la oferta no cumple las reglas de publicación.
        """
        if self.status is not ServiceStatus.DRAFT:
            raise ValueError("Solo un servicio en borrador se puede publicar.")
        ensure_publishable(self, now)
        return replace(
            self,
            status=ServiceStatus.PUBLISHED,
            version=self.version + 1,
            published_at=now,
            updated_at=now,
        )


def _declared_instant(instant: datetime, time_zone: str) -> str:
    """Instante almacenado (UTC) expresado con el desfase de su zona declarada."""
    try:
        return instant.astimezone(ZoneInfo(time_zone)).isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        # La zona ya no es válida: se conserva el instante y la regla de la zona lo informa.
        return instant.isoformat()


def _as_input(offer: ServiceOffer) -> OfferInput:
    """La oferta almacenada como si llegara de nuevo, para revalidarla con las mismas reglas."""
    return OfferInput(
        name=offer.name,
        description=offer.description,
        category=offer.category.value,
        modality=offer.modality.value,
        price=PriceInput(
            amount=str(offer.price.amount),
            currency=offer.price.currency.value,
            billing_unit=offer.price.billing_unit.value,
        ),
        availability=[
            IntervalInput(
                starts_at=_declared_instant(interval.starts_at, interval.time_zone),
                ends_at=_declared_instant(interval.ends_at, interval.time_zone),
                time_zone=interval.time_zone,
                capacity=interval.capacity,
            )
            for interval in offer.availability
        ],
    )


def ensure_publishable(offer: ServiceOffer, now: datetime) -> None:
    """Reglas de publicación de ADR-027 (HU019 CA1, CA2), informando todas a la vez.

    Revalida la oferta almacenada con las mismas reglas de la creación. Los intervalos
    vencidos (inicio no futuro) se ignoran, pero hace falta por lo menos uno futuro; sin
    ninguno, `{field: "availability", issue: "required"}`.
    """
    violations: list[Violation] = []
    try:
        validate_offer(_as_input(offer), now)
    except InvalidServiceOffer as error:
        # `not_in_future` solo lo produce el inicio de un intervalo: vencido, se ignora.
        violations.extend(
            item for item in error.violations if item.issue is not Issue.NOT_IN_FUTURE
        )
    if not any(interval.starts_at > now for interval in offer.availability):
        violations.append(Violation("availability", Issue.REQUIRED))
    if violations:
        raise InvalidServiceOffer(violations)
