"""Tarifa informativa del evento (HU025, ADR-040).

- `amount`: cadena decimal con exactamente dos decimales entre `0.00` (evento gratuito) y
  `99999999.99`; `currency`: solo `COP`. Un importe sin dos decimales es `unknown_value`,
  uno con más es `too_many_decimals` y uno negativo o mayor que el máximo, `out_of_range`.
- `expected_version`: entero positivo; debe coincidir con la versión vigente del evento.
- La tarifa no es un cobro: no genera pago, estado de pago ni reembolso, y no cambia el
  `applied_fee` de las inscripciones existentes (ADR-032).

`evento.tarifa_actualizada.v1`: tópico `sportapp.eventos.v1`, agregado `event`, clave de
orden `event_id`; el payload lleva el estado completo del evento, tarifa incluida.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from events_management.domain.validation import InvalidSportEvent, Issue, Violation

EVENT_TYPE = "evento.tarifa_actualizada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "event"

CURRENCY = "COP"
AMOUNT_MIN = Decimal("0.00")
AMOUNT_MAX = Decimal("99999999.99")
DECIMAL_PLACES = 2
_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


class VersionConflict(Exception):
    """`expected_version` no es la versión vigente del evento."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(str(current_version))
        self.current_version = current_version


@dataclass(frozen=True)
class EventFee:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class FeeInput:
    """Solicitud tal como llega, con las violaciones de forma ya detectadas por el transporte."""

    amount: str | None
    currency: str | None
    expected_version: int | None
    transport_violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class FeeChange:
    """Tarifa validada (`None` = retirarla) y la versión que el organizador vio."""

    fee: EventFee | None
    expected_version: int


def _amount(value: str | None, violations: list[Violation]) -> Decimal | None:
    if value is None:
        return None
    if not _DECIMAL.match(value):
        violations.append(Violation("amount", Issue.UNKNOWN_VALUE))
        return None
    amount = Decimal(value)
    decimals = len(value.partition(".")[2])
    if decimals > DECIMAL_PLACES:
        violations.append(Violation("amount", Issue.TOO_MANY_DECIMALS))
        return None
    if decimals < DECIMAL_PLACES:
        violations.append(Violation("amount", Issue.UNKNOWN_VALUE))
        return None
    if not AMOUNT_MIN <= amount <= AMOUNT_MAX:
        violations.append(Violation("amount", Issue.OUT_OF_RANGE))
        return None
    return amount


def _version(value: int | None, violations: list[Violation]) -> int | None:
    if value is not None and value < 1:
        violations.append(Violation("expected_version", Issue.OUT_OF_RANGE))
        return None
    return value


def validate_fee(raw: FeeInput) -> FeeChange:
    """Tarifa a asignar o `InvalidSportEvent` con todas las reglas incumplidas (CA2)."""
    violations = list(raw.transport_violations)
    amount = _amount(raw.amount, violations)
    if raw.currency is not None and raw.currency != CURRENCY:
        violations.append(Violation("currency", Issue.UNSUPPORTED_CURRENCY))
    version = _version(raw.expected_version, violations)
    if violations or amount is None or raw.currency is None or version is None:
        raise InvalidSportEvent(violations)
    return FeeChange(fee=EventFee(amount=amount, currency=CURRENCY), expected_version=version)


def validate_removal(expected_version: int | None, violations: list[Violation]) -> FeeChange:
    """Retiro de la tarifa (`DELETE`): solo exige una versión válida."""
    found = list(violations)
    version = _version(expected_version, found)
    if found or version is None:
        raise InvalidSportEvent(found)
    return FeeChange(fee=None, expected_version=version)


def fee_document(fee: EventFee | None) -> dict[str, Any] | None:
    """`{amount, currency}` con dos decimales, o `null` sin tarifa (ADR-040)."""
    return None if fee is None else {"amount": f"{fee.amount:.2f}", "currency": fee.currency}
