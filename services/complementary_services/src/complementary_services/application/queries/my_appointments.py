"""HU040: citas propias (`GET /v1/me/appointments`, ADR-046 §4).

- `athlete`: sus citas; `service_provider`: las citas de sus servicios (fuente
  `OWN_APPOINTMENT` del calendario integrado de HU018). Otro actor → `permission_required`.
  Consultar no exige la capacidad: la cita sigue consultable aunque cambie el plan (CA6).
- `from`/`to` opcionales y en pareja (instantes ISO 8601 con desfase): acotan `starts_at`
  a `[from, to)`; solo uno → `required` en el que falta; `to ≤ from` → `ends_before_starts`.
- `status`: `BOOKED` o `CANCELLED_BY_ATHLETE`; sin valor, todos (las canceladas no se
  ocultan: nada se elimina silenciosamente, CA6).
- Orden por `starts_at` y `appointment_id` ascendentes con cursor keyset opaco; `limit` de
  1 a 100 (20 por defecto). Cada elemento trae el resumen del servicio.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from complementary_services.application.errors import PermissionRequired
from complementary_services.application.ports import (
    AppointmentPosition,
    AppointmentReader,
    AppointmentView,
    Caller,
    OwnAppointmentsQuery,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.appointments.appointment import AppointmentStatus
from complementary_services.domain.validation import Issue

ATHLETE = "athlete"
SERVICE_PROVIDER = "service_provider"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class MyAppointmentsFilters:
    """Parámetros tal como llegan; `limit` ya es entero (el transporte informa si no lo es)."""

    starts_from: str | None = None
    starts_before: str | None = None
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class MyAppointmentsPage:
    items: tuple[AppointmentView, ...]
    next_cursor: str | None


def encode_cursor(position: AppointmentPosition) -> str:
    raw = json.dumps(
        {"s": position.starts_at.isoformat(), "i": str(position.appointment_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> AppointmentPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        starts_at = datetime.fromisoformat(document["s"])
        appointment_id = UUID(document["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE) from None
    if starts_at.tzinfo is None:
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE)
    return AppointmentPosition(starts_at=starts_at, appointment_id=appointment_id)


def _instant(raw: str, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidServicesQuery(field, Issue.UNKNOWN_VALUE) from None
    if value.tzinfo is None:
        raise InvalidServicesQuery(field, Issue.UNKNOWN_VALUE)
    return value


def _period(filters: MyAppointmentsFilters) -> tuple[datetime | None, datetime | None]:
    raw_from, raw_to = filters.starts_from, filters.starts_before
    if raw_from is None and raw_to is None:
        return None, None
    if raw_from is None:
        raise InvalidServicesQuery("from", Issue.REQUIRED)
    if raw_to is None:
        raise InvalidServicesQuery("to", Issue.REQUIRED)
    starts_from, starts_before = _instant(raw_from, "from"), _instant(raw_to, "to")
    if starts_before <= starts_from:
        raise InvalidServicesQuery("to", Issue.ENDS_BEFORE_STARTS)
    return starts_from, starts_before


def _status(raw: str | None) -> AppointmentStatus | None:
    if raw is None:
        return None
    try:
        return AppointmentStatus(raw)
    except ValueError:
        raise InvalidServicesQuery("status", Issue.UNKNOWN_VALUE) from None


class ListMyAppointments:
    def __init__(self, *, reader: AppointmentReader) -> None:
        self._reader = reader

    def authorize(self, caller: Caller) -> None:
        """Deportista o profesional; el transporte lo llama antes de leer los parámetros."""
        if caller.actor_type not in {ATHLETE, SERVICE_PROVIDER}:
            raise PermissionRequired()

    def __call__(self, caller: Caller, filters: MyAppointmentsFilters) -> MyAppointmentsPage:
        self.authorize(caller)
        starts_from, starts_before = _period(filters)
        status = _status(filters.status)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidServicesQuery("limit", Issue.OUT_OF_RANGE)
        after = decode_cursor(filters.cursor) if filters.cursor is not None else None
        athlete = caller.actor_type == ATHLETE
        views = self._reader.list_own(
            OwnAppointmentsQuery(
                athlete_id=caller.user_id if athlete else None,
                provider_id=None if athlete else caller.user_id,
                starts_from=starts_from,
                starts_before=starts_before,
                status=status,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = tuple(views[: filters.limit])
        next_cursor = None
        if len(views) > filters.limit:
            last = page[-1].appointment
            next_cursor = encode_cursor(AppointmentPosition(last.starts_at, last.appointment_id))
        return MyAppointmentsPage(items=page, next_cursor=next_cursor)
