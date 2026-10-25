"""Reglas de composición del calendario integrado web (HU018, ADR-039 con ADR-031), sin
transporte.

- Periodo: `from`/`to` instantes ISO 8601 con desfase; ambos ausentes → mes UTC en curso
  (`to` es el último segundo del mes); `from` ≤ `to`; ventana de hasta 92 días.
- Eventos e inscripciones aparecen si su intervalo `[starts_at, ends_at]` se superpone con el
  periodo (misma regla que `GET /v1/events`); un intervalo de servicio `[starts_at, ends_at)`,
  si empieza a más tardar en `to` y termina después de `from`.
- Las sesiones planificadas se piden por fecha: las de `from` y `to` en el desfase recibido
  (la zona del navegador), o en UTC si se tomó el mes por defecto.
- `occurs_on`: fecha de inicio en la `time_zone` del elemento, o la `scheduled_date` de la
  sesión.
- Orden: `occurs_on`, `starts_at` (nulos primero), `source`, `item_id`.
- Cada fuente aporta como máximo 200 elementos (los primeros en ese orden) y marca
  `truncated` cuando el periodo contiene más.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_WINDOW = timedelta(days=92)
SOURCE_LIMIT = 200

OWN_EVENT = "OWN_EVENT"
EVENT_PUBLISHED = "EVENT_PUBLISHED"
EVENT_REGISTRATION = "EVENT_REGISTRATION"
TRAINING_PLAN_SESSION = "TRAINING_PLAN_SESSION"
OWN_SERVICE_AVAILABILITY = "OWN_SERVICE_AVAILABILITY"
ASSOCIATED_SERVICE_AVAILABILITY = "ASSOCIATED_SERVICE_AVAILABILITY"

# Estados visibles: un evento cancelado u otro estado futuro no aparece (CA4).
EVENT_STATUSES = frozenset({"DRAFT", "PUBLISHED"})
SERVICE_STATUSES = frozenset({"DRAFT", "PUBLISHED"})


class InvalidPeriod(Exception):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class Period:
    period_from: datetime  # con el desfase recibido
    period_to: datetime

    @property
    def date_from(self) -> date:
        return self.period_from.date()

    @property
    def date_to(self) -> date:
        return self.period_to.date()


def _instant(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidPeriod(field, "invalid_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidPeriod(field, "invalid_datetime")
    return parsed


def current_month(now: datetime) -> Period:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return Period(start, next_month - timedelta(seconds=1))


def normalize_period(raw_from: str | None, raw_to: str | None, now: datetime) -> Period:
    if raw_from is None and raw_to is None:
        return current_month(now)
    if raw_from is None or raw_to is None:
        raise InvalidPeriod("from" if raw_from is None else "to", "required")
    period = Period(_instant(raw_from, "from"), _instant(raw_to, "to"))
    if period.period_to < period.period_from:
        raise InvalidPeriod("to", "ends_before_starts")
    if period.period_to - period.period_from > MAX_WINDOW:
        raise InvalidPeriod("to", "out_of_range")
    return period


def utc_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def instant(value: Any) -> datetime:
    """Instante de una respuesta interna; sin desfase no es un dato válido del contrato."""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("instante sin desfase")
    return parsed


def event_overlaps(period: Period, starts_at: datetime, ends_at: datetime) -> bool:
    return starts_at <= period.period_to and ends_at >= period.period_from


def interval_overlaps(period: Period, starts_at: datetime, ends_at: datetime) -> bool:
    return starts_at <= period.period_to and ends_at > period.period_from


def _local_date(value: datetime, time_zone: str) -> date:
    try:
        return value.astimezone(ZoneInfo(time_zone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return value.astimezone(UTC).date()


def _timed(
    source: str, key: str, starts_at: datetime, ends_at: datetime, time_zone: str
) -> dict[str, Any]:
    return {
        "item_id": f"{source}:{key}",
        "source": source,
        "occurs_on": _local_date(starts_at, time_zone).isoformat(),
        "starts_at": utc_instant(starts_at),
        "ends_at": utc_instant(ends_at),
        "time_zone": time_zone,
    }


def _location(location: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": location["name"],
        "city": location["city"],
        "country_code": location["country_code"],
    }


def _event(source: str, event: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        **_timed(
            source,
            str(event["event_id"]),
            instant(event["starts_at"]),
            instant(event["ends_at"]),
            str(event["time_zone"]),
        ),
        "status": status,
        "title": event["name"],
        "sport": event["discipline"],
        "location": _location(event["location"]),
        "reference": {"event_id": event["event_id"]},
    }


def own_event_item(event: dict[str, Any]) -> dict[str, Any]:
    """Evento propio del organizador (`GET /v1/me/events`): `DRAFT` o `PUBLISHED`."""
    return _event(OWN_EVENT, event, str(event["status"]))


def published_event_item(event: dict[str, Any]) -> dict[str, Any]:
    return _event(EVENT_PUBLISHED, event, "PUBLISHED")


def registration_item(registration: dict[str, Any]) -> dict[str, Any]:
    """Inscripción propia (HU041); el resumen del evento no trae disciplina."""
    event = registration["event"]
    return {
        **_timed(
            EVENT_REGISTRATION,
            str(registration["registration_id"]),
            instant(event["starts_at"]),
            instant(event["ends_at"]),
            str(event["time_zone"]),
        ),
        "status": "REGISTERED",
        "title": event["name"],
        "location": _location(event["location"]),
        "reference": {
            "event_id": registration["event_id"],
            "registration_id": registration["registration_id"],
        },
    }


def session_item(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": f"{TRAINING_PLAN_SESSION}:{session['session_id']}",
        "source": TRAINING_PLAN_SESSION,
        "occurs_on": date.fromisoformat(str(session["scheduled_date"])).isoformat(),
        "starts_at": None,
        "ends_at": None,
        "time_zone": None,
        "status": "ASSIGNED",
        "title": session["plan_title"],
        "sport": session["sport"],
        "duration_minutes": session["duration_minutes"],
        "rpe": session["rpe"],
        "reference": {"plan_id": session["plan_id"], "session_id": session["session_id"]},
    }


def _interval(
    source: str, service: dict[str, Any], interval: dict[str, Any], status: str
) -> dict[str, Any]:
    starts_at = instant(interval["starts_at"])
    # El intervalo no tiene identificador propio: servicio + inicio lo identifican.
    return {
        **_timed(
            source,
            f"{service['service_id']}:{utc_instant(starts_at)}",
            starts_at,
            instant(interval["ends_at"]),
            str(interval["time_zone"]),
        ),
        "status": status,
        "title": service["name"],
        "category": service["category"],
        "modality": service["modality"],
        "capacity": interval["capacity"],
    }


def own_service_item(service: dict[str, Any], interval: dict[str, Any]) -> dict[str, Any]:
    """Intervalo de un servicio propio (`GET /v1/me/services`) con el estado del servicio."""
    return {
        **_interval(OWN_SERVICE_AVAILABILITY, service, interval, str(service["status"])),
        "reference": {"service_id": service["service_id"]},
    }


def associated_service_item(
    service: dict[str, Any], interval: dict[str, Any], association_id: str
) -> dict[str, Any]:
    """Intervalo de un servicio asociado al deportista (catálogo publicado de HU020)."""
    return {
        **_interval(ASSOCIATED_SERVICE_AVAILABILITY, service, interval, "AVAILABLE"),
        "reference": {"service_id": service["service_id"], "association_id": association_id},
    }


def availability_windows(period: Period, span: timedelta) -> list[tuple[datetime, datetime]]:
    """Ventanas consecutivas `[desde, hasta)` de como máximo `span` que cubren el periodo: el
    catálogo de disponibilidad admite hasta 31 días y exige `to` posterior a `from`."""
    windows, start = [], period.period_from.astimezone(UTC)
    end = period.period_to.astimezone(UTC)
    while start < end:
        following = min(start + span, end)
        windows.append((start, following))
        start = following
    return windows


_NO_TIME = datetime.min.replace(tzinfo=UTC)


def _order(item: dict[str, Any]) -> tuple[str, int, datetime, str, str]:
    starts_at = item["starts_at"]
    # Nulos primero: una sesión sin hora va antes que los elementos con hora del mismo día.
    return (
        item["occurs_on"],
        0 if starts_at is None else 1,
        _NO_TIME if starts_at is None else datetime.fromisoformat(starts_at),
        item["source"],
        item["item_id"],
    )


def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=_order)


def capped(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Los primeros 200 de una fuente en el orden del calendario y si había más."""
    return ordered(items)[:SOURCE_LIMIT], len(items) > SOURCE_LIMIT
