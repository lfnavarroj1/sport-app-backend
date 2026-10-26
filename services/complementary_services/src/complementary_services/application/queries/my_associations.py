"""HU017: asociaciones propias del deportista (`GET /v1/me/service-associations`, ADR-038).

- Solo `athlete` con `services.sports_medicine.access` (consultada ante Afiliación).
- `status`: `ACTIVE` (por defecto, «Mis servicios asociados»), `INACTIVE` o `ALL`; con
  `ALL` se conserva la evidencia de las desasociadas (CA5).
- Orden por `associated_at` y `association_id` descendentes con cursor keyset opaco;
  `limit` de 1 a 100 (20 por defecto). Cada elemento trae el resumen del servicio.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from complementary_services.application.commands.associate_service import MedicalAccess
from complementary_services.application.ports import (
    AssociationPosition,
    AssociationReader,
    AssociationView,
    Caller,
    OwnAssociationsQuery,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.association.association import AssociationStatus
from complementary_services.domain.validation import Issue

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
ALL = "ALL"


@dataclass(frozen=True)
class MyAssociationsFilters:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class MyAssociationsPage:
    items: tuple[AssociationView, ...]
    next_cursor: str | None


def encode_cursor(position: AssociationPosition) -> str:
    raw = json.dumps(
        {"a": position.associated_at.isoformat(), "i": str(position.association_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> AssociationPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        associated_at = datetime.fromisoformat(document["a"])
        association_id = UUID(document["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE) from None
    if associated_at.tzinfo is None:
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE)
    return AssociationPosition(associated_at=associated_at, association_id=association_id)


def _status(raw: str | None) -> AssociationStatus | None:
    if raw is None:
        return AssociationStatus.ACTIVE
    if raw == ALL:
        return None
    try:
        return AssociationStatus(raw)
    except ValueError:
        raise InvalidServicesQuery("status", Issue.UNKNOWN_VALUE) from None


class ListMyAssociations:
    def __init__(self, *, reader: AssociationReader, access: MedicalAccess) -> None:
        self._reader = reader
        self._access = access

    def authorize(self, caller: Caller) -> None:
        """Actor y capacidad; el transporte lo llama antes de leer los parámetros."""
        self._access.authorize(caller)

    def __call__(self, caller: Caller, filters: MyAssociationsFilters) -> MyAssociationsPage:
        self.authorize(caller)
        return self.execute(caller, filters)

    def execute(self, caller: Caller, filters: MyAssociationsFilters) -> MyAssociationsPage:
        """La consulta con quien llama ya autorizado."""
        status = _status(filters.status)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidServicesQuery("limit", Issue.OUT_OF_RANGE)
        after = decode_cursor(filters.cursor) if filters.cursor is not None else None
        views = self._reader.list_own(
            OwnAssociationsQuery(
                athlete_id=caller.user_id, status=status, after=after, limit=filters.limit + 1
            )
        )
        page = tuple(views[: filters.limit])
        next_cursor = None
        if len(views) > filters.limit:
            last = page[-1].association
            next_cursor = encode_cursor(
                AssociationPosition(last.associated_at, last.association_id)
            )
        return MyAssociationsPage(items=page, next_cursor=next_cursor)
