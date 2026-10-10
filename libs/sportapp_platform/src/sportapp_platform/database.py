"""Acceso técnico a PostgreSQL mediante SQLAlchemy.

Cada componente crea sus propios motores con sus propias credenciales; este
módulo no conoce bases ni tablas de ningún dominio.
"""

import logging

from pydantic import SecretStr
from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from sportapp_platform.config import DatabasePoolSettings

_logger = logging.getLogger("sportapp.database")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_metadata() -> MetaData:
    return MetaData(naming_convention=NAMING_CONVENTION)


def create_database_engine(url: SecretStr, pool: DatabasePoolSettings) -> Engine:
    """Crea un motor perezoso: no abre conexiones hasta el primer uso."""
    return create_engine(
        url.get_secret_value(),
        pool_size=pool.db_pool_size,
        max_overflow=pool.db_max_overflow,
        pool_pre_ping=True,
        connect_args={"connect_timeout": pool.db_connect_timeout_seconds},
    )


class DatabaseReadinessCheck:
    """Comprueba que la base responda a `SELECT 1`."""

    def __init__(self, name: str, engine: Engine) -> None:
        self._name = name
        self._engine = engine

    @property
    def name(self) -> str:
        return self._name

    def __call__(self) -> bool:
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            _logger.warning(
                "database.unavailable",
                extra={"fields": {"check": self._name, "error_type": type(exc).__name__}},
            )
            return False
        return True
