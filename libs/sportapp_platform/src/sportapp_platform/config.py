"""Configuración tipada por ambiente, validada al iniciar cada componente."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

POSTGRES_URL_SCHEME = "postgresql+psycopg://"


class ConfigurationError(RuntimeError):
    """La configuración obligatoria falta o es inválida."""


def _require_postgres_url(value: SecretStr) -> SecretStr:
    if not value.get_secret_value().startswith(POSTGRES_URL_SCHEME):
        raise ValueError(f"debe iniciar con {POSTGRES_URL_SCHEME}")
    return value


PostgresUrl = Annotated[SecretStr, AfterValidator(_require_postgres_url)]


class ServiceSettings(BaseSettings):
    """Variables comunes a toda API, BFF o worker."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    app_env: Environment
    log_level: LogLevel = "INFO"
    http_port: int = Field(default=8000, ge=1, le=65535)
    service_version: str = "0.0.0-local"


class DatabasePoolSettings(BaseSettings):
    """Límites del pool de conexiones hacia PostgreSQL."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)


def load_settings[T: BaseSettings](settings_class: type[T]) -> T:
    """Carga la configuración desde el ambiente y falla sin revelar valores.

    Los errores de Pydantic incluyen los valores recibidos; por eso solo se
    informa el nombre de cada variable afectada.
    """
    try:
        return settings_class()
    except ValidationError as exc:
        variables = sorted(
            {".".join(str(part) for part in error["loc"]).upper() for error in exc.errors()}
        )
        raise ConfigurationError(
            "Configuración inválida o incompleta: " + ", ".join(variables)
        ) from None
