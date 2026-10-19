"""Modelos SQLAlchemy de la base de lectura (proyecciones reconstruibles y `processed_event`).

Nunca se exponen como respuestas públicas.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sportapp_platform.database import new_metadata

DIRECTORY_EMAIL_UNIQUE_CONSTRAINT = "uq_user_directory_email"
# Secciones de `athlete_profile_view` (ADR-034); HU006 reutiliza `sport_habits`.
PROFILE_SECTIONS = ("demographic", "physiological", "sport_habits")


class ReadBase(DeclarativeBase):
    metadata = new_metadata()


class UserDirectoryRow(ReadBase):
    """Directorio mínimo de identidad (HU010): correo normalizado → `user_id` y `actor_type`.

    Lo mantiene `usuarios-read-projector` a partir de `usuario.registrado.v1` (refactor
    de HU001, creado con HU005). Nunca contiene contraseña, hash ni tokens.
    """

    __tablename__ = "user_directory"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('athlete', 'organizer', 'service_provider')", name="actor_type"
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    # HU011 (CA5): nombre visible para la lista de deportistas del profesional. Nulo solo en
    # filas proyectadas antes de `0004` hasta que corre `workers.directory_backfill`.
    full_name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _section_constraints() -> tuple[CheckConstraint, ...]:
    # Una sección y su versión se proyectan juntas; la versión empieza en 1.
    return tuple(
        CheckConstraint(
            f"({section} IS NULL) = ({section}_version IS NULL)"
            f" AND ({section}_version IS NULL OR {section}_version >= 1)",
            name=f"{section}_version",
        )
        for section in PROFILE_SECTIONS
    )


class AthleteProfileViewRow(ReadBase):
    """Perfil del deportista por secciones (HU005/HU006, ADR-034).

    Cada sección es el estado completo publicado en su evento, sin `user_id`; se
    reemplaza solo con una versión mayor que la proyectada. Contiene datos
    demográficos y de salud: nunca se registra en logs ni se expone tal cual.
    """

    __tablename__ = "athlete_profile_view"
    __table_args__ = _section_constraints()

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    demographic: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    physiological: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sport_habits: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    demographic_version: Mapped[int | None] = mapped_column(BigInteger)
    physiological_version: Mapped[int | None] = mapped_column(BigInteger)
    sport_habits_version: Mapped[int | None] = mapped_column(BigInteger)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"AthleteProfileViewRow(user_id={self.user_id})"


class ProcessedEventRow(ReadBase):
    """Deduplicación del proyector por `event_id` (entrega al menos una vez, CMP-001)."""

    __tablename__ = "processed_event"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    projection_name: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID]
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
