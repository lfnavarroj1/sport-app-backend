"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

DIRECTORY_EMAIL_UNIQUE_CONSTRAINT = "uq_user_directory_email"


class ReadBase(DeclarativeBase):
    metadata = new_metadata()


class UserDirectoryRow(ReadBase):
    """Directorio mínimo de identidad (HU010): correo normalizado → `user_id` y `actor_type`.

    Lo mantiene `usuarios-read-projector` a partir de `usuario.registrado.v1`; el
    proyector es un refactor pendiente de HU001. Nunca contiene contraseña, hash
    ni tokens.
    """

    __tablename__ = "user_directory"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('athlete', 'organizer', 'service_provider')", name="actor_type"
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
