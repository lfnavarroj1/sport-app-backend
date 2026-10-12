"""Modelos SQLAlchemy de la base de escritura (fuente de verdad y `outbox_event`).

Nunca se exponen como respuestas públicas.
"""

from sqlalchemy.orm import DeclarativeBase

from sportapp_platform.database import new_metadata


class WriteBase(DeclarativeBase):
    metadata = new_metadata()
