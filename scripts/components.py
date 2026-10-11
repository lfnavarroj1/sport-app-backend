"""Inventario de componentes desplegables del repositorio backend."""

from dataclasses import dataclass, field

CONTRACT_DATABASE_URL = "postgresql+psycopg://contract@127.0.0.1:1/contract"


@dataclass(frozen=True)
class Component:
    name: str
    package: str
    directory: str
    # Variables mínimas para construir la app al exportar su OpenAPI (sin conectarse).
    contract_environment: dict[str, str] = field(default_factory=dict)

    @property
    def factory(self) -> str:
        return f"{self.package}.main:create_app"

    @property
    def image(self) -> str:
        return f"sportapp/{self.name}"


_CQRS = {
    "DATABASE_WRITE_URL": CONTRACT_DATABASE_URL,
    "DATABASE_READ_URL": CONTRACT_DATABASE_URL,
}
_SINGLE = {"DATABASE_URL": CONTRACT_DATABASE_URL}
_USERS = {**_CQRS, "REGISTRATION_POLICIES_VERSION": "contract"}
_BFF_WEB = {"USERS_MANAGEMENT_BASE_URL": "http://users-management.invalid"}

COMPONENTS = (
    Component("users-management", "users_management", "services/users_management", _USERS),
    Component(
        "affiliation-management",
        "affiliation_management",
        "services/affiliation_management",
        _SINGLE,
    ),
    Component(
        "complementary-services",
        "complementary_services",
        "services/complementary_services",
        _SINGLE,
    ),
    Component("training-management", "training_management", "services/training_management", _CQRS),
    Component("events-management", "events_management", "services/events_management", _CQRS),
    Component(
        "third-party-management", "third_party_management", "services/third_party_management"
    ),
    Component("bff-web", "bff_web", "bff/web", _BFF_WEB),
    Component("bff-mobile", "bff_mobile", "bff/mobile"),
)

# Paquetes con pruebas propias (componentes desplegables + librería técnica).
TEST_PACKAGES = ("libs/sportapp_platform", *(component.directory for component in COMPONENTS))


def by_name(name: str) -> Component:
    for component in COMPONENTS:
        if component.name == name:
            return component
    names = ", ".join(component.name for component in COMPONENTS)
    raise SystemExit(f"Componente desconocido: {name}. Opciones: {names}")
