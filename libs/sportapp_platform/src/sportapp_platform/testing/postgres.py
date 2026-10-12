"""Bases PostgreSQL temporales para pruebas de integración.

Requiere `TEST_DATABASE_URL` con un usuario capaz de crear bases, por ejemplo
`postgresql+psycopg://postgres@127.0.0.1:55432/postgres`. Si falta, la prueba
falla de forma explícita: una integración omitida no cuenta como aprobada.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, make_url, pool, text

from sportapp_platform.config import POSTGRES_URL_SCHEME, ConfigurationError

TEST_DATABASE_URL_VARIABLE = "TEST_DATABASE_URL"


def admin_database_url() -> str:
    value = os.environ.get(TEST_DATABASE_URL_VARIABLE, "")
    if not value.startswith(POSTGRES_URL_SCHEME):
        raise ConfigurationError(
            f"Configuración inválida o incompleta: {TEST_DATABASE_URL_VARIABLE} "
            "(las pruebas de integración necesitan PostgreSQL real)"
        )
    return value


@contextmanager
def temporary_database(prefix: str) -> Iterator[str]:
    """Crea una base vacía y aislada; la elimina al terminar."""
    admin_url = admin_database_url()
    name = f"it_{prefix}_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(admin_url, poolclass=pool.NullPool, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()
