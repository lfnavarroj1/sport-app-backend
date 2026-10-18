"""HU011 (CA5, decisión del 2026-10-18): deportistas que autorizaron al profesional.

- Solo el profesional con el permiso `training.history.view_assigned` (el mismo que le
  permite consultar el historial) y solo sus propias autorizaciones vigentes, leídas de la
  vista proyectada: una revocación lo retira de la lista cuando el proyector la aplica.
- Orden estable `authorized_at` desc, `athlete_id` desc y cursor keyset opaco.
- Devuelve solo identificadores y la fecha de autorización; los datos personales no se
  copian a Entrenamientos.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from training_management.application.errors import PermissionRequired
from training_management.application.ports import (
    AuthorizedAthleteEntry,
    AuthorizedAthletePosition,
    AuthorizedAthleteReader,
    AuthorizedAthletesQuery,
    Caller,
    PermissionPolicy,
)
from training_management.application.queries.training_history import (
    DEFAULT_LIMIT,
    HISTORY_VIEW_PERMISSION,
    MAX_LIMIT,
    InvalidHistoryQuery,
)


@dataclass(frozen=True)
class AuthorizedAthletesPage:
    items: tuple[AuthorizedAthleteEntry, ...]
    next_cursor: str | None


def encode_cursor(position: AuthorizedAthletePosition) -> str:
    raw = json.dumps(
        {"a": position.authorized_at.isoformat(), "i": str(position.athlete_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> AuthorizedAthletePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        authorized_at = datetime.fromisoformat(document["a"])
        athlete_id = UUID(document["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidHistoryQuery("cursor", "invalid_cursor") from None
    if authorized_at.tzinfo is None:
        raise InvalidHistoryQuery("cursor", "invalid_cursor")
    return AuthorizedAthletePosition(authorized_at=authorized_at, athlete_id=athlete_id)


class GetAuthorizedAthletes:
    def __init__(self, *, reader: AuthorizedAthleteReader, permissions: PermissionPolicy) -> None:
        self._reader = reader
        self._permissions = permissions

    def __call__(
        self, caller: Caller, *, limit: int = DEFAULT_LIMIT, cursor: str | None = None
    ) -> AuthorizedAthletesPage:
        if not self._permissions.allows(caller.actor_type, HISTORY_VIEW_PERMISSION):
            raise PermissionRequired()
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidHistoryQuery("limit", "out_of_range")
        after = decode_cursor(cursor) if cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        entries = self._reader.list(
            AuthorizedAthletesQuery(professional_id=caller.user_id, after=after, limit=limit + 1)
        )
        page = entries[:limit]
        next_cursor = (
            encode_cursor(AuthorizedAthletePosition(page[-1].authorized_at, page[-1].athlete_id))
            if len(entries) > limit
            else None
        )
        return AuthorizedAthletesPage(items=tuple(page), next_cursor=next_cursor)
