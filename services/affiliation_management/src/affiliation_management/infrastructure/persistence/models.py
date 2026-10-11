"""Modelos SQLAlchemy de la base de única del dominio.

Nunca se exponen como respuestas públicas.
"""

from sqlalchemy.orm import DeclarativeBase

from sportapp_platform.database import new_metadata


class Base(DeclarativeBase):
    metadata = new_metadata()
