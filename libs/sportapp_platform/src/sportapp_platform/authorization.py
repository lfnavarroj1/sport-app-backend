"""Contrato versionado de permisos por actor (ADR-025): carga y consulta.

Capacidad técnica compartida: lee `contracts/authorization/actor-permissions.v1.json`,
que es código revisado. No contiene reglas de un dominio: cada servicio sigue
validando propiedad, relación y estado de sus recursos.

En la imagen OCI el contrato se copia a una ruta fija y `ACTOR_PERMISSIONS_PATH`
la indica; en el repositorio se localiza desde la raíz.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sportapp_platform.config import ConfigurationError

ACTOR_PERMISSIONS_ENV = "ACTOR_PERMISSIONS_PATH"
CONTRACT_RELATIVE_PATH = Path("contracts") / "authorization" / "actor-permissions.v1.json"
CHANNELS = frozenset({"web", "mobile"})


@dataclass(frozen=True)
class ActorPermissions:
    version: str
    # actor → [(permiso, canales habilitados)]
    grants: dict[str, tuple[tuple[str, frozenset[str]], ...]]

    def is_known_actor(self, actor_type: object) -> bool:
        return isinstance(actor_type, str) and actor_type in self.grants

    def for_actor(self, actor_type: str, channel: str) -> tuple[str, ...]:
        """Permisos del actor habilitados en el canal; vacío si el actor no los tiene."""
        return tuple(
            sorted(
                code for code, channels in self.grants.get(actor_type, ()) if channel in channels
            )
        )


def resolve_actor_permissions_path(start: Path | None = None) -> Path:
    configured = os.environ.get(ACTOR_PERMISSIONS_ENV)
    if configured:
        return Path(configured)
    origin = (start or Path(__file__)).resolve()
    for directory in (origin, *origin.parents):
        candidate = directory / CONTRACT_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    raise ConfigurationError(f"Configuración inválida o incompleta: {ACTOR_PERMISSIONS_ENV}")


def _invalid(reason: str) -> ConfigurationError:
    return ConfigurationError(f"Contrato de permisos inválido ({reason}): {ACTOR_PERMISSIONS_ENV}")


def load_actor_permissions(path: Path) -> ActorPermissions:
    """Carga y valida el contrato; un contrato ausente o mal formado detiene el arranque."""
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _invalid("no se pudo leer") from None
    if not isinstance(document, dict) or not isinstance(document.get("version"), str):
        raise _invalid("falta version")
    actors = document.get("actors")
    if not isinstance(actors, dict) or not actors:
        raise _invalid("falta actors")
    grants: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {}
    for actor, entries in actors.items():
        if not isinstance(entries, list):
            raise _invalid(f"actor {actor}")
        parsed: list[tuple[str, frozenset[str]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise _invalid(f"permiso de {actor}")
            code, channels = entry.get("permission"), entry.get("channels")
            if not isinstance(code, str) or not code:
                raise _invalid(f"permiso de {actor}")
            if not isinstance(channels, list) or not channels or not set(channels) <= CHANNELS:
                raise _invalid(f"canales de {code}")
            parsed.append((code, frozenset(channels)))
        grants[str(actor)] = tuple(parsed)
    return ActorPermissions(version=document["version"], grants=grants)
