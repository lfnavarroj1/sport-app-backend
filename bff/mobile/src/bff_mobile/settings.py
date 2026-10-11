"""Configuración de `bff_mobile`, validada al iniciar."""

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import ServiceSettings


class BffMobileSettings(ServiceSettings):
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
