"""Configuración de `bff_web`, validada al iniciar."""

from typing import Annotated
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
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple):
            return tuple(_validate_origin(str(item)) for item in value)
        return value
