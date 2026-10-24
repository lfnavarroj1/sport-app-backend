"""HU002 (ADR-035): reglas del cuerpo de `PUT /v1/me/account`.

Todo o nada, con el catálogo de `issue` de ADR-034 (`details[{field, issue}]`):

- `expected_version` obligatorio (la cuenta siempre existe), entero ≥ 1;
- `full_name` obligatorio, espacios internos colapsados y exteriores recortados, de 1 a 120;
- `email` obligatorio, recortado y en minúsculas, ≤ 254. El formato (`unknown_value`) lo
  comprueba el transporte con el mismo validador de HU001.
"""

from dataclasses import dataclass

from users_management.domain.account.model import EMAIL_MAX_LENGTH, FULL_NAME_MAX_LENGTH
from users_management.domain.profile.validation import (
    Collector,
    InvalidProfileSection,
    Issue,
    Violation,
)


@dataclass(frozen=True)
class AccountChangeInput:
    """Valores con la forma JSON correcta; lo demás ya está en `transport_violations`."""

    expected_version: int | None = None
    full_name: str | None = None
    email: str | None = None
    transport_violations: tuple[Violation, ...] = ()

    def __repr__(self) -> str:
        # Nombre y correo son datos personales: nunca en trazas.
        return f"AccountChangeInput(expected_version={self.expected_version})"


@dataclass(frozen=True)
class AccountChange:
    expected_version: int
    full_name: str
    email: str

    def __repr__(self) -> str:
        return f"AccountChange(expected_version={self.expected_version})"

    def normalized(self) -> dict[str, object]:
        """Forma canónica para la huella de idempotencia."""
        return {
            "expected_version": self.expected_version,
            "full_name": self.full_name,
            "email": self.email,
        }


def validate_account_change(change: AccountChangeInput) -> AccountChange:
    collector = Collector(change.transport_violations)
    expected_version = full_name = email = None
    if collector.required(change.expected_version, "expected_version"):
        expected_version = collector.expected_version(change.expected_version)
    if collector.required(change.full_name, "full_name") and change.full_name is not None:
        full_name = " ".join(change.full_name.split())
        if not 1 <= len(full_name) <= FULL_NAME_MAX_LENGTH:
            collector.add("full_name", Issue.INVALID_LENGTH)
            full_name = None
    if collector.required(change.email, "email") and change.email is not None:
        email = change.email.strip().lower()
        if not 1 <= len(email) <= EMAIL_MAX_LENGTH:
            collector.add("email", Issue.INVALID_LENGTH)
            email = None
    collector.raise_if_any()
    if expected_version is None or full_name is None or email is None:
        raise InvalidProfileSection([])  # Inalcanzable: la falta ya se informó arriba.
    return AccountChange(expected_version=expected_version, full_name=full_name, email=email)
