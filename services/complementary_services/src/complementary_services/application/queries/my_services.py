"""Servicios propios del profesional (ADR-038, soporte de HU019 y HU018).

- «Mis servicios»: solo los propios, con filtro opcional `status` (`DRAFT` o `PUBLISHED`),
  ordenados por `updated_at` y `service_id` descendentes con cursor keyset opaco; `limit`
  de 1 a 100 (20 por defecto).
- Detalle propio: un servicio inexistente o de otro profesional es `service_not_found`.
Solo `service_provider`; la oferta completa incluye la disponibilidad y `published_at`.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from complementary_services.application.errors import PermissionRequired, ServiceNotFound
from complementary_services.application.ports import (
    Caller,
    OwnServicesQuery,
    ServicePosition,
    ServiceReader,
    ServiceView,
)
from complementary_services.domain.catalog.offer import ServiceStatus
from complementary_services.domain.validation import Issue

SERVICE_PROVIDER = "service_provider"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class InvalidServicesQuery(Exception):
    """Parámetro de consulta inválido; `field` sin el prefijo `query.` del transporte."""

    code = "validation_error"

    def __init__(self, field: str, issue: Issue) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class MyServicesFilters:
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class MyServicesPage:
    items: tuple[ServiceView, ...]
    next_cursor: str | None


def encode_cursor(position: ServicePosition) -> str:
    raw = json.dumps(
        {"u": position.updated_at.isoformat(), "s": str(position.service_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> ServicePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        updated_at = datetime.fromisoformat(document["u"])
        service_id = UUID(document["s"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE) from None
    if updated_at.tzinfo is None:
        raise InvalidServicesQuery("cursor", Issue.UNKNOWN_VALUE)
    return ServicePosition(updated_at=updated_at, service_id=service_id)


def _require_provider(caller: Caller) -> None:
    if caller.actor_type != SERVICE_PROVIDER:
        raise PermissionRequired()


class ListMyServices:
    def __init__(self, *, reader: ServiceReader) -> None:
        self._reader = reader

    def authorize(self, caller: Caller) -> None:
        """Solo el profesional; también antes de leer los parámetros en el transporte."""
        _require_provider(caller)

    def __call__(self, caller: Caller, filters: MyServicesFilters) -> MyServicesPage:
        _require_provider(caller)
        status: ServiceStatus | None = None
        if filters.status is not None:
            try:
                status = ServiceStatus(filters.status)
            except ValueError:
                raise InvalidServicesQuery("status", Issue.UNKNOWN_VALUE) from None
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidServicesQuery("limit", Issue.OUT_OF_RANGE)
        after = decode_cursor(filters.cursor) if filters.cursor is not None else None
        offers = self._reader.list_own(
            OwnServicesQuery(
                provider_id=caller.user_id, status=status, after=after, limit=filters.limit + 1
            )
        )
        page = offers[: filters.limit]
        next_cursor = (
            encode_cursor(ServicePosition(page[-1].updated_at, page[-1].service_id))
            if len(offers) > filters.limit
            else None
        )
        return MyServicesPage(
            items=tuple(ServiceView(offer) for offer in page), next_cursor=next_cursor
        )


class GetMyService:
    def __init__(self, *, reader: ServiceReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller, service_id: UUID) -> ServiceView:
        _require_provider(caller)
        offer = self._reader.own_service(caller.user_id, service_id)
        if offer is None:
            raise ServiceNotFound()
        return ServiceView(offer)
