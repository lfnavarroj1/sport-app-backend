"""Configuración de `bff_mobile`, validada al iniciar."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import ServiceSettings


class BffMobileSettings(ServiceSettings):
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # HU003 CA7-CA10 (ADR-024/025): composición de `GET /v1/me/capabilities`.
    affiliation_management_base_url: AnyHttpUrl
    # HU032: sesiones de entrenamiento; HU035: sesiones planificadas del calendario.
    training_management_base_url: AnyHttpUrl
    # HU035: eventos publicados del calendario.
    events_management_base_url: AnyHttpUrl
    # HU029: disponibilidad de servicios publicados para las sugerencias de acompañamiento.
    complementary_services_base_url: AnyHttpUrl
    # HU031 (ADR-042): vinculación, estado y sincronización con Strava y su callback.
    third_party_management_base_url: AnyHttpUrl
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None
