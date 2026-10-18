"""Relleno único de `user_directory.full_name` (HU011, CA5; decisión del 2026-10-18).

Ejecución: `python -m users_management.workers.directory_backfill` (Job administrativo,
después de la migración de lectura `0004_user_directory_full_name`).

Las filas proyectadas antes de `0004` no tienen nombre y sus eventos ya se procesaron, por
lo que el proyector no los vuelve a aplicar. Este comando es la reconstrucción controlada de
CMP-001: lee el nombre de la fuente de verdad (`user_account`, base de escritura) y solo
completa filas con `full_name` nulo. Es idempotente y se puede repetir; no toca otras
columnas ni crea filas, y los logs llevan solo conteos, nunca nombres.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Engine, select, update

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.infrastructure.persistence_write.models import UserAccountRow
from users_management.settings import DirectoryBackfillSettings

BATCH_SIZE = 500

_logger = logging.getLogger("sportapp.backfill")


def _pending(read_engine: Engine, after: UUID | None) -> Sequence[UUID]:
    statement = select(UserDirectoryRow.user_id).where(UserDirectoryRow.full_name.is_(None))
    if after is not None:
        statement = statement.where(UserDirectoryRow.user_id > after)
    with read_engine.connect() as connection:
        return connection.scalars(
            statement.order_by(UserDirectoryRow.user_id).limit(BATCH_SIZE)
        ).all()


def backfill(write_engine: Engine, read_engine: Engine) -> int:
    """Completa `full_name` de las filas pendientes; devuelve cuántas actualizó."""
    updated = 0
    after: UUID | None = None
    while batch := _pending(read_engine, after):
        after = batch[-1]
        with write_engine.connect() as connection:
            names = dict(
                connection.execute(
                    select(UserAccountRow.user_id, UserAccountRow.full_name).where(
                        UserAccountRow.user_id.in_(batch)
                    )
                ).all()
            )
        with read_engine.begin() as connection:
            for user_id, full_name in names.items():
                result = connection.execute(
                    update(UserDirectoryRow)
                    .where(
                        UserDirectoryRow.user_id == user_id,
                        UserDirectoryRow.full_name.is_(None),
                    )
                    .values(full_name=full_name)
                )
                updated += result.rowcount
    return updated


def main() -> None:
    settings = load_settings(DirectoryBackfillSettings)
    configure_logging(
        service="users-management-directory-backfill",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    write_engine = create_database_engine(settings.database_write_url, settings)
    read_engine = create_database_engine(settings.database_read_url, settings)
    try:
        updated = backfill(write_engine, read_engine)
    finally:
        write_engine.dispose()
        read_engine.dispose()
    _logger.info("backfill.completed", extra={"fields": {"updated": updated}})


if __name__ == "__main__":
    main()
