"""Configuración de `complementary_services`, validada al iniciar."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings


class ComplementaryServicesSettings(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl
    # HU015: identidad desde el JWKS de users-management.
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None
