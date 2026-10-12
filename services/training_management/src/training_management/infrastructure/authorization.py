"""Política de permisos por actor a partir del contrato `actor-permissions.v1` (ADR-025)."""

from sportapp_platform.authorization import CHANNELS, ActorPermissions


class ContractPermissionPolicy:
    """El servicio no distingue canal: basta con que el permiso exista para el actor."""

    def __init__(self, permissions: ActorPermissions) -> None:
        self._granted = {
            actor: frozenset(
                code for channel in CHANNELS for code in permissions.for_actor(actor, channel)
            )
            for actor in permissions.grants
        }

    def allows(self, actor_type: str, permission: str) -> bool:
        return permission in self._granted.get(actor_type, frozenset())
