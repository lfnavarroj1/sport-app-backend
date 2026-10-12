"""HU011: historial de sesiones terminadas (decisiones del 2026-10-12).

- El deportista consulta su propio historial; ADR-024 no define capacidad para HU011.
- El profesional necesita el permiso `training.history.view_assigned` y una
  autorización vigente del deportista, leída de la vista proyectada (CA3). Una
  revocación surte efecto cuando el proyector la aplica.
- Filtros por periodo (fechas de fin, inclusivas, en UTC) y deporte; orden estable
  `ended_at` desc, `session_id` desc, y paginación por cursor (keyset) que no pierde
  ni repite registros entre páginas aunque lleguen sesiones nuevas (CA1, CA4).
- Sin historial devuelve una colección vacía con los filtros disponibles (CA2).
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
    HistoryPosition,
    HistoryQuery,
    HistoryReader,
    PermissionPolicy,
    SessionHistoryEntry,
)
from training_management.domain.plans.model import Sport

HISTORY_VIEW_PERMISSION = "training.history.view_assigned"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_audit = logging.getLogger("sportapp.audit")


class InvalidHistoryQuery(Exception):
    """Filtro, límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class HistoryFilters:
    date_from: date | None = None
    date_to: date | None = None
    sport: Sport | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    items: tuple[SessionHistoryEntry, ...]
    next_cursor: str | None
    available_sports: tuple[str, ...]


def encode_cursor(position: HistoryPosition) -> str:
    raw = json.dumps(
        {"e": position.ended_at.isoformat(), "s": str(position.session_id)}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> HistoryPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        ended_at = datetime.fromisoformat(document["e"])
        session_id = UUID(document["s"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidHistoryQuery("cursor", "invalid_cursor") from None
    if ended_at.tzinfo is None:
        raise InvalidHistoryQuery("cursor", "invalid_cursor")
    return HistoryPosition(ended_at=ended_at, session_id=session_id)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


class GetTrainingHistory:
    def __init__(
        self,
        *,
        reader: HistoryReader,
        authorizations: AuthorizationReader,
        permissions: PermissionPolicy,
    ) -> None:
        self._reader = reader
        self._authorizations = authorizations
        self._permissions = permissions

    def own(self, caller: Caller, filters: HistoryFilters) -> HistoryPage:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        return self._page(caller.user_id, filters)

    def of_athlete(self, caller: Caller, athlete_id: UUID, filters: HistoryFilters) -> HistoryPage:
        """Acceso de un profesional a datos de otra persona: se audita el resultado."""
        try:
            if not self._permissions.allows(caller.actor_type, HISTORY_VIEW_PERMISSION):
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
        _audit.info(
            "audit.training_history_access",
            extra={
                "fields": {
                    "result": result,
                    "professional_id": str(caller.user_id),
                    "athlete_id": str(athlete_id),
                }
            },
        )

    def _page(self, athlete_id: UUID, filters: HistoryFilters) -> HistoryPage:
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidHistoryQuery("limit", "out_of_range")
        if filters.date_from and filters.date_to and filters.date_to < filters.date_from:
            raise InvalidHistoryQuery("to", "to_before_from")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        entries = self._reader.list(
            HistoryQuery(
                athlete_id=athlete_id,
                ended_from=_start_of(filters.date_from) if filters.date_from else None,
                ended_before=_start_of(filters.date_to + timedelta(days=1))
                if filters.date_to
                else None,
                sport=filters.sport.value if filters.sport else None,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = entries[: filters.limit]
        next_cursor = (
            encode_cursor(HistoryPosition(page[-1].ended_at, page[-1].session_id))
            if len(entries) > filters.limit
            else None
        )
        return HistoryPage(
            items=tuple(page),
            next_cursor=next_cursor,
            available_sports=tuple(sport.value for sport in Sport),
        )
