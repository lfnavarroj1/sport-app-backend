"""Ejecución de migraciones Alembic por historial.

Cada base (escritura, lectura o única) tiene su propio historial dentro del
paquete del servicio propietario. Las migraciones se ejecutan mediante un job
controlado, nunca al arrancar las réplicas:

    python -m sportapp_platform.migrations upgrade \
        --script-location users_management.infrastructure.persistence_write:migrations \
        --url-env DATABASE_WRITE_URL
"""

import argparse
import os
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import MetaData, create_engine, pool

from sportapp_platform.config import POSTGRES_URL_SCHEME, ConfigurationError

_URL_ATTRIBUTE = "database_url"


def _alembic_config(script_location: str, database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", script_location)
    # Se usa un atributo y no `sqlalchemy.url` para no interpolar ni registrar la URL.
    config.attributes[_URL_ATTRIBUTE] = database_url
    return config


def upgrade(script_location: str, database_url: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(script_location, database_url), revision)


def downgrade(script_location: str, database_url: str, revision: str) -> None:
    """Reversión controlada a una revisión explícita (nunca implícita)."""
    command.downgrade(_alembic_config(script_location, database_url), revision)


def current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def run_migrations(context: Any, target_metadata: MetaData) -> None:
    """Cuerpo común de cada `env.py`; solo admite modo en línea."""
    if context.is_offline_mode():
        raise ConfigurationError("SportApp solo ejecuta migraciones en línea.")
    database_url = context.config.attributes.get(_URL_ATTRIBUTE)
    if not database_url:
        raise ConfigurationError("Falta la URL de base de datos para la migración.")
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


def _url_from_environment(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value.startswith(POSTGRES_URL_SCHEME):
        raise ConfigurationError(f"Configuración inválida o incompleta: {variable}")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sportapp_platform.migrations")
    subcommands = parser.add_subparsers(dest="action", required=True)
    upgrade_parser = subcommands.add_parser("upgrade", help="Aplica migraciones hasta la revisión")
    upgrade_parser.add_argument("--script-location", required=True)
    upgrade_parser.add_argument("--url-env", required=True)
    upgrade_parser.add_argument("--revision", default="head")
    arguments = parser.parse_args(argv)
    upgrade(arguments.script_location, _url_from_environment(arguments.url_env), arguments.revision)


if __name__ == "__main__":
    main()
