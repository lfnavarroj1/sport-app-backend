"""Inventario de componentes desplegables del repositorio backend.

Solo usa la biblioteca estándar al importarse: la CI ejecuta `task.py` con
`uv run --no-project` (jobs `secrets` e `image`), sin las dependencias del proyecto.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

CONTRACT_DATABASE_URL = "postgresql+psycopg://contract@127.0.0.1:1/contract"


@dataclass(frozen=True)
class Component:
    name: str
    package: str
    directory: str
    # Variables mínimas para construir la app al exportar su OpenAPI (sin conectarse).
    contract_environment: dict[str, str] = field(default_factory=dict)
    # Necesita `JWT_PRIVATE_KEY` para construirse; se genera una desechable al exportar.
    needs_signing_key: bool = False

    def openapi_environment(self) -> dict[str, str]:
        environment = dict(self.contract_environment)
        if self.needs_signing_key:
            environment["JWT_PRIVATE_KEY"] = new_signing_key_pem()
        return environment

    @property
    def factory(self) -> str:
        return f"{self.package}.main:create_app"

    @property
    def image(self) -> str:
        return f"sportapp/{self.name}"


def build_command(
    component: Component,
    *,
    dockerfile: str,
    references: Sequence[str],
    version: str,
    revision: str,
) -> list[str]:
    """`docker build` de un componente: lo usan `task.py build` y `release.py images`.

    Una sola construcción por componente; `references` son todas las etiquetas que recibe la
    misma imagen (p. ej., la versión y el commit).
    """
    command = [
        "docker",
        "build",
        "--file",
        dockerfile,
        "--build-arg",
        f"PACKAGE={component.name}",
        "--build-arg",
        f"APP_FACTORY={component.factory}",
        "--build-arg",
        f"SERVICE_VERSION={version}",
        "--label",
        f"org.opencontainers.image.revision={revision}",
        "--label",
        f"org.opencontainers.image.version={version}",
        "--label",
        f"org.opencontainers.image.title={component.name}",
    ]
    for reference in references:
        command += ["--tag", reference]
    return [*command, "."]


_CQRS = {
    "DATABASE_WRITE_URL": CONTRACT_DATABASE_URL,
    "DATABASE_READ_URL": CONTRACT_DATABASE_URL,
}
_SINGLE = {"DATABASE_URL": CONTRACT_DATABASE_URL}


def new_signing_key_pem() -> str:
    """Clave Ed25519 nueva en PEM (PKCS#8). Para el contrato es desechable: no cambia el OpenAPI.

    `cryptography` se importa aquí y no al inicio del módulo (ver docstring del módulo).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return (
        Ed25519PrivateKey.generate()
        .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        .decode("ascii")
    )


_USERS = {
    **_CQRS,
    "REGISTRATION_POLICIES_VERSION": "contract",
    "HEALTH_DATA_CONSENT_VERSION": "contract",
}
_AFFILIATION = {
    **_SINGLE,
    "AFFILIATION_EXPECTED_CATALOG_VERSION": "contract",
    # HU016: JWKS de Usuarios para verificar el token del deportista.
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
}
_BFF = {
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
    "AFFILIATION_MANAGEMENT_BASE_URL": "http://affiliation-management.invalid",
    "TRAINING_MANAGEMENT_BASE_URL": "http://training-management.invalid",
    "COMPLEMENTARY_SERVICES_BASE_URL": "http://complementary-services.invalid",
    "EVENTS_MANAGEMENT_BASE_URL": "http://events-management.invalid",
}
# HU031: bff-mobile reenvía las operaciones de Strava a Terceros (bff-web no lo necesita).
_BFF_MOBILE = {
    **_BFF,
    "THIRD_PARTY_MANAGEMENT_BASE_URL": "http://third-party-management.invalid",
}
_COMPLEMENTARY = {
    **_SINGLE,
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
    "AFFILIATION_MANAGEMENT_BASE_URL": "http://affiliation-management.invalid",
}
_EVENTS = {
    **_CQRS,
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
    "AFFILIATION_MANAGEMENT_BASE_URL": "http://affiliation-management.invalid",
}
_TRAINING = {
    **_CQRS,
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
    "AFFILIATION_MANAGEMENT_BASE_URL": "http://affiliation-management.invalid",
    # HU042: consentimiento de datos de salud de los reportes.
    "HEALTH_DATA_CONSENT_VERSION": "contract",
}
# HU031 (ADR-042): base única, JWKS de Usuarios y capacidad en Afiliación. Las STRAVA_* son
# opcionales al construir la app (sin ellas solo falla `/health/ready`): no cambian el OpenAPI.
_THIRD_PARTY = {
    **_SINGLE,
    "USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid",
    "AFFILIATION_MANAGEMENT_BASE_URL": "http://affiliation-management.invalid",
}

COMPONENTS = (
    Component(
        "users-management",
        "users_management",
        "services/users_management",
        _USERS,
        needs_signing_key=True,
    ),
    Component(
        "affiliation-management",
        "affiliation_management",
        "services/affiliation_management",
        _AFFILIATION,
    ),
    Component(
        "complementary-services",
        "complementary_services",
        "services/complementary_services",
        _COMPLEMENTARY,
    ),
    Component(
        "training-management", "training_management", "services/training_management", _TRAINING
    ),
    Component("events-management", "events_management", "services/events_management", _EVENTS),
    Component(
        "third-party-management",
        "third_party_management",
        "services/third_party_management",
        _THIRD_PARTY,
    ),
    Component("bff-web", "bff_web", "bff/web", _BFF),
    Component("bff-mobile", "bff_mobile", "bff/mobile", _BFF_MOBILE),
)

# Paquetes con pruebas propias (componentes desplegables + librería técnica).
TEST_PACKAGES = (
    "libs/sportapp_platform",
    *(component.directory for component in COMPONENTS),
    # Comandos de release (`scripts/release.py`); no son código de producción.
    "scripts",
)


def by_name(name: str) -> Component:
    for component in COMPONENTS:
        if component.name == name:
            return component
    names = ", ".join(component.name for component in COMPONENTS)
    raise SystemExit(f"Componente desconocido: {name}. Opciones: {names}")
