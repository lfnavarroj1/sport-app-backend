"""Configuración de `bff_web`, validada al iniciar."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import NoDecode

from sportapp_platform.config import ServiceSettings


def _validate_origin(origin: str) -> str:
    """Un origen es esquema + host (+ puerto), sin ruta: así lo envía el navegador."""
    parts = urlsplit(origin)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "*" in origin
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ValueError("cada origen debe tener la forma http(s)://host[:puerto], sin ruta")
    return origin


class BffWebSettings(ServiceSettings):
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # Orígenes del cliente web autorizados por CORS, separados por comas. Vacío: ninguno.
    # También son los únicos `Origin` aceptados en las operaciones de sesión (CSRF).
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    # `SameSite` de la cookie del refresh token. `strict` sirve cuando web y BFF comparten
    # sitio (localhost con distinto puerto, o dominio propio común); `none` solo si están en
    # sitios distintos, y Safari la bloquea.
    refresh_cookie_same_site: Literal["strict", "lax", "none"] = "strict"
    # HU003 CA7-CA10 (ADR-024/025): composición de `GET /v1/me/capabilities`.
    affiliation_management_base_url: AnyHttpUrl
    # HU010: autorización del deportista y planes de entrenamiento.
    training_management_base_url: AnyHttpUrl
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple):
            return tuple(_validate_origin(str(item)) for item in value)
        return value
