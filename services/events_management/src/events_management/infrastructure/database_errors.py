"""Traducción de una base inalcanzable a `StoreUnavailable` (HU039).

La API de la bandeja y de las preferencias responde `503 upstream_unavailable` en lugar de un
`500` cuando PostgreSQL no responde; nunca expone detalles del motor.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from events_management.application.ports import StoreUnavailable


@contextmanager
def unavailable_on_failure() -> Iterator[None]:
    try:
        yield
    except (OperationalError, InterfaceError, PoolTimeoutError):
        raise StoreUnavailable() from None
