"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from sqlalchemy.orm import DeclarativeBase

from sportapp_platform.database import new_metadata


class ReadBase(DeclarativeBase):
    metadata = new_metadata()
