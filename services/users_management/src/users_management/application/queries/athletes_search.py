"""ADR-045 §8: destinatarios de las alertas de eventos (contrato interno, HU039).

`eventos-notificaciones-worker` consulta por lotes los deportistas cuyos hábitos deportivos
(HU006) incluyen alguno de los deportes del evento publicado y cuya ciudad y país (HU005)
coinciden con los del evento. Eventos traduce antes la disciplina a códigos del catálogo
(alias de ADR-041); Usuarios no conoce eventos ni disciplinas.

- `sports`: de 1 a 4 códigos únicos del catálogo de HU006 sin `other` (ADR-041: `other` no
  genera coincidencia) → `unknown_value`.
- `city`: de 2 a 80 caracteres tras recortar; se compara normalizada (`normalize_term`).
- `country_code`: ISO 3166-1 alfa-2 en mayúsculas, igualdad exacta.
- `limit`: de 1 a 1.000 (por defecto 1.000); `cursor` opaco, `null` en la primera página.

Solo cuentas `active` de tipo `athlete`, en orden `user_id` ascendente (keyset). Cada
búsqueda se audita (`audit.athletes_search`) con el consumidor, el resultado y la cantidad
devuelta, nunca con la ciudad, los deportes ni los identificadores. El consumidor es fijo,
`events-management`, el único de HU039.
"""

import base64
import binascii
import json
import logging
from typing import Any
from uuid import UUID

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import (
    AthleteSearchCriteria,
    AthleteSearchPage,
    AthleteSearchReader,
)
from users_management.domain.profile.countries import COUNTRY_CODES
from users_management.domain.profile.sport_habits import Sport
from users_management.domain.profile.terms import normalize_term
from users_management.domain.profile.validation import Issue

CONSUMER = "events-management"
SEARCH_FIELDS = ("sports", "city", "country_code", "limit", "cursor")
SPORTS_MIN, SPORTS_MAX = 1, 4
CITY_MIN, CITY_MAX = 2, 80
LIMIT_MIN, LIMIT_MAX = 1, 1000
DEFAULT_LIMIT = LIMIT_MAX
CURSOR_MAX = 512
# `other` no es un deporte concreto y nunca genera coincidencia (ADR-041).
SEARCHABLE_SPORTS = tuple(sport.value for sport in Sport if sport is not Sport.OTHER)
INVALID_CURSOR = "invalid_cursor"

_FIELD_PREFIX = "body."
_audit = logging.getLogger("sportapp.audit")


class InvalidAthleteSearch(Exception):
    """La búsqueda incumple el contrato; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[tuple[str | None, str]]) -> None:
        self.violations = tuple(violations)
        super().__init__(", ".join(f"{field}: {issue}" for field, issue in self.violations))


def _field(name: str) -> str:
    return f"{_FIELD_PREFIX}{name}"


def encode_cursor(user_id: UUID) -> str:
    raw = json.dumps({"u": str(user_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> UUID | None:
    """Último `user_id` de la página anterior; `None` si el cursor no es uno emitido aquí."""
    if not cursor or len(cursor) > CURSOR_MAX:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return UUID(document["u"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        return None


class _Problems:
    def __init__(self) -> None:
        self.items: list[tuple[str | None, str]] = []

    def add(self, field: str | None, issue: str) -> None:
        self.items.append((field, issue))


def _sports(value: object, problems: _Problems) -> tuple[str, ...]:
    if value is None:
        problems.add(_field("sports"), Issue.REQUIRED.value)
        return ()
    if not isinstance(value, list):
        problems.add(_field("sports"), Issue.UNKNOWN_VALUE.value)
        return ()
    if len(value) < SPORTS_MIN:
        # Mismo criterio que HU006: una lista obligatoria vacía es `required`.
        problems.add(_field("sports"), Issue.REQUIRED.value)
        return ()
    if len(value) > SPORTS_MAX:
        problems.add(_field("sports"), Issue.TOO_MANY_ITEMS.value)
        return ()
    found: list[str] = []
    for position, item in enumerate(value):
        path = _field(f"sports.{position}")
        if not isinstance(item, str) or item not in SEARCHABLE_SPORTS:
            problems.add(path, Issue.UNKNOWN_VALUE.value)
        elif item in found:
            problems.add(path, Issue.DUPLICATE_VALUE.value)
        else:
            found.append(item)
    # Orden del catálogo: la misma búsqueda da los mismos filtros sea cual sea el orden.
    return tuple(sport for sport in SEARCHABLE_SPORTS if sport in found)


def _city(value: object, problems: _Problems) -> str | None:
    if value is None:
        problems.add(_field("city"), Issue.REQUIRED.value)
        return None
    if not isinstance(value, str):
        problems.add(_field("city"), Issue.UNKNOWN_VALUE.value)
        return None
    if not CITY_MIN <= len(value.strip()) <= CITY_MAX:
        problems.add(_field("city"), Issue.INVALID_LENGTH.value)
        return None
    return normalize_term(value)


def _country(value: object, problems: _Problems) -> str | None:
    if value is None:
        problems.add(_field("country_code"), Issue.REQUIRED.value)
        return None
    if not isinstance(value, str) or value not in COUNTRY_CODES:
        problems.add(_field("country_code"), Issue.UNKNOWN_VALUE.value)
        return None
    return value


def _limit(value: object, problems: _Problems) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not isinstance(value, int) or isinstance(value, bool):
        problems.add(_field("limit"), Issue.UNKNOWN_VALUE.value)
        return DEFAULT_LIMIT
    if not LIMIT_MIN <= value <= LIMIT_MAX:
        problems.add(_field("limit"), Issue.OUT_OF_RANGE.value)
        return DEFAULT_LIMIT
    return value


def _after(value: object, problems: _Problems) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.add(_field("cursor"), Issue.UNKNOWN_VALUE.value)
        return None
    after = decode_cursor(value)
    if after is None:
        problems.add(_field("cursor"), INVALID_CURSOR)
    return after


def parse_athlete_search(body: object) -> AthleteSearchCriteria:
    """Valida todo o nada el cuerpo JSON ya decodificado (`None` si venía vacío).

    Ninguna violación contiene el valor recibido (la ciudad es un dato personal del perfil).
    """
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        raise InvalidAthleteSearch([(None, issue.value)])
    document: dict[str, Any] = body
    problems = _Problems()
    for name in document:
        if name not in SEARCH_FIELDS:
            problems.add(_field(name), Issue.FIELD_NOT_ALLOWED.value)
    sports = _sports(document.get("sports"), problems)
    city = _city(document.get("city"), problems)
    country_code = _country(document.get("country_code"), problems)
    limit = _limit(document.get("limit"), problems)
    after = _after(document.get("cursor"), problems)
    if problems.items or city is None or country_code is None or not sports:
        raise InvalidAthleteSearch(problems.items)
    return AthleteSearchCriteria(
        sports=sports, city=city, country_code=country_code, limit=limit, after=after
    )


class SearchAthletes:
    def __init__(self, reader: AthleteSearchReader) -> None:
        self._reader = reader

    def __call__(self, body: object) -> tuple[AthleteSearchPage, str | None]:
        """Página de identificadores y el cursor de la siguiente (`None` si es la última)."""
        try:
            criteria = parse_athlete_search(body)
        except InvalidAthleteSearch as error:
            self.reject(error.code)
            raise
        try:
            page = self._reader.search(criteria)
        except ReadModelUnavailable:
            self._audit("unavailable", count=0)
            raise
        next_cursor = encode_cursor(page.user_ids[-1]) if page.has_more and page.user_ids else None
        self._audit("found", count=len(page.user_ids))
        return page, next_cursor

    def reject(self, reason: str) -> None:
        """Una solicitud que no debía llegar (con el token de un cliente) o inválida."""
        self._audit("rejected", count=0, reason=reason)

    @staticmethod
    def _audit(result: str, *, count: int, reason: str | None = None) -> None:
        fields: dict[str, str | int] = {"consumer": CONSUMER, "result": result, "count": count}
        if reason is not None:
            fields["reason"] = reason
        _audit.info("audit.athletes_search", extra={"fields": fields})
