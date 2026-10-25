"""HU012: historial de molestias, incapacidades o lesiones (ADR-037).

- El deportista consulta sus propios reportes; es una opción base, sin capacidad de plan.
- El profesional necesita el permiso `health.history.view_assigned` y una autorización
  vigente del deportista (HU010), leída de la vista proyectada como en HU011 (CA2, CA3). Una
  revocación surte efecto cuando el proyector la aplica. Sin autorización responde lo mismo
  exista o no el deportista.
- Se audita `audit.health_history_access` en cada consulta del profesional y en cada
  rechazo, sin ningún dato de los reportes (CA3, CA5).
- Lee `health_report_view` (HU042). Filtros por periodo (fechas de `occurred_at`, inclusivas,
  en UTC), tipo y severidad; orden `occurred_at` desc, `report_id` desc, y paginación por
  cursor (keyset) que no pierde ni repite reportes (CA1, CA6).
- Sin reportes devuelve una colección vacía explícita (CA4).
"""

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from training_management.application.access import ATHLETE
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.ports import (
    AuthorizationReader,
    Caller,
    HealthReportHistoryEntry,
    HealthReportHistoryQuery,
    HealthReportHistoryReader,
    HealthReportPosition,
    PermissionPolicy,
)
from training_management.domain.health.model import ReportType, Severity

HEALTH_HISTORY_VIEW_PERMISSION = "health.history.view_assigned"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_audit = logging.getLogger("sportapp.audit")


class InvalidHealthHistoryQuery(Exception):
    """Periodo, límite o cursor inválido; se informa el parámetro afectado (CA6)."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class HealthHistoryFilters:
    date_from: date | None = None
    date_to: date | None = None
    type: ReportType | None = None
    severity: Severity | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class HealthHistoryPage:
    items: tuple[HealthReportHistoryEntry, ...]
    next_cursor: str | None


def encode_cursor(position: HealthReportPosition) -> str:
    raw = json.dumps(
        {"o": position.occurred_at.isoformat(), "r": str(position.report_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> HealthReportPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        occurred_at = datetime.fromisoformat(document["o"])
        report_id = UUID(document["r"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidHealthHistoryQuery("cursor", "invalid_cursor") from None
    if occurred_at.tzinfo is None:
        raise InvalidHealthHistoryQuery("cursor", "invalid_cursor")
    return HealthReportPosition(occurred_at=occurred_at, report_id=report_id)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


class GetHealthHistory:
    def __init__(
        self,
        *,
        reader: HealthReportHistoryReader,
        authorizations: AuthorizationReader,
        permissions: PermissionPolicy,
    ) -> None:
        self._reader = reader
        self._authorizations = authorizations
        self._permissions = permissions

    def own(self, caller: Caller, filters: HealthHistoryFilters) -> HealthHistoryPage:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        return self._page(caller.user_id, filters)

    def of_athlete(
        self, caller: Caller, athlete_id: UUID, filters: HealthHistoryFilters
    ) -> HealthHistoryPage:
        """Acceso de un profesional a datos de salud de otra persona: se audita el resultado."""
        try:
            if not self._permissions.allows(caller.actor_type, HEALTH_HISTORY_VIEW_PERMISSION):
                raise PermissionRequired()
            authorization = self._authorizations.get(athlete_id, caller.user_id)
            if authorization is None or not authorization.is_active:
                raise AthleteAuthorizationRequired()
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            self._record(caller, athlete_id, f"rejected:{error.code}")
            raise
        page = self._page(athlete_id, filters)
        self._record(caller, athlete_id, "granted")
        return page

    @staticmethod
    def _record(caller: Caller, athlete_id: UUID, result: str) -> None:
        # Solo identificadores y resultado: ningún valor de los reportes (CA5).
        _audit.info(
            "audit.health_history_access",
            extra={
                "fields": {
                    "result": result,
                    "professional_id": str(caller.user_id),
                    "actor_type": caller.actor_type,
                    "athlete_id": str(athlete_id),
                }
            },
        )

    def _page(self, athlete_id: UUID, filters: HealthHistoryFilters) -> HealthHistoryPage:
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidHealthHistoryQuery("limit", "out_of_range")
        if filters.date_from and filters.date_to and filters.date_to < filters.date_from:
            raise InvalidHealthHistoryQuery("to", "to_before_from")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        entries = self._reader.list(
            HealthReportHistoryQuery(
                athlete_id=athlete_id,
                occurred_from=_start_of(filters.date_from) if filters.date_from else None,
                occurred_before=_start_of(filters.date_to + timedelta(days=1))
                if filters.date_to
                else None,
                type=filters.type.value if filters.type else None,
                severity=filters.severity.value if filters.severity else None,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = entries[: filters.limit]
        next_cursor = (
            encode_cursor(HealthReportPosition(page[-1].occurred_at, page[-1].report_id))
            if len(entries) > filters.limit
            else None
        )
        return HealthHistoryPage(items=tuple(page), next_cursor=next_cursor)
