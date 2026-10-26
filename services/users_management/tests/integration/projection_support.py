"""Base de lectura migrada, proyector real y eventos sintéticos de `sportapp.usuarios.v1`."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.projections.users_projection import UsersProjector
from users_management.domain.profile.events import EVENT_TYPE_BY_SECTION, ProfileSection
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    ProcessedEventRow,
    UserDirectoryRow,
)
from users_management.infrastructure.persistence_read.nutrition_restrictions import (
    SqlAlchemyNutritionRestrictionsReader,
)
from users_management.infrastructure.persistence_read.profile import (
    SqlAlchemyAthleteProfileReader,
)
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork,
)

READ_HISTORY = "users_management.infrastructure.persistence_read:migrations"
# Valores sintéticos fáciles de buscar en los logs capturados.
SYNTHETIC_WEIGHT = "68.5"
SYNTHETIC_TEMPERATURE = "36.6"


@dataclass
class ReadDatabase:
    url: str
    engine: Engine
    sessions: sessionmaker[Session]

    def projector(self) -> UsersProjector:
        return UsersProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.sessions),
            clock=SystemClock(),
        )

    def reader(self) -> SqlAlchemyAthleteProfileReader:
        return SqlAlchemyAthleteProfileReader(self.sessions)

    def restrictions(self) -> SqlAlchemyNutritionRestrictionsReader:
        return SqlAlchemyNutritionRestrictionsReader(self.sessions)

    def count(
        self, model: type[ProcessedEventRow | AthleteProfileViewRow | UserDirectoryRow]
    ) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def directory(self, user_id: UUID) -> UserDirectoryRow | None:
        with self.sessions() as session:
            return session.get(UserDirectoryRow, user_id)

    def profile_rows(self) -> list[dict[str, Any]]:
        """Vista completa sin `projected_at` (depende del instante de proyección)."""
        with self.sessions() as session:
            rows = session.scalars(select(AthleteProfileViewRow).order_by("user_id")).all()
            return [
                {
                    "user_id": row.user_id,
                    "demographic": row.demographic,
                    "physiological": row.physiological,
                    "sport_habits": row.sport_habits,
                    "nutrition_habits": row.nutrition_habits,
                    "nutrition_habits_version": row.nutrition_habits_version,
                    "versions": (
                        row.demographic_version,
                        row.physiological_version,
                        row.sport_habits_version,
                    ),
                }
                for row in rows
            ]


@contextmanager
def read_database(prefix: str) -> Iterator[ReadDatabase]:
    with temporary_database(prefix) as url:
        upgrade(READ_HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        try:
            yield ReadDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
        finally:
            engine.dispose()


def _payload(section: ProfileSection, user_id: UUID, version: int) -> dict[str, Any]:
    common = {
        "user_id": str(user_id),
        "version": version,
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": f"2026-10-16T12:{version % 60:02d}:00Z",
    }
    if section is ProfileSection.DEMOGRAPHIC:
        return {
            **common,
            "birth_date": "1990-05-14",
            "gender": "FEMALE",
            "country_code": "CO",
            "city": f"Ciudad {version}",
            "marital_status": None,
            "occupation": None,
            "income_range": "PREFER_NOT_TO_SAY",
        }
    if section is ProfileSection.SPORT_HABITS:
        return {
            **common,
            "sports": [
                {
                    "sport": "running",
                    "frequency_per_week": 1 + version % 7,
                    "session_duration_minutes": 45,
                    "intensity": "MODERATE",
                    "level": "INTERMEDIATE",
                    "years_practicing": None,
                }
            ],
            "availability": {"days": ["MON", "SAT"], "time_slots": ["MORNING"]},
            "primary_goal": None,
        }
    if section is ProfileSection.NUTRITION_HABITS:
        return {
            **common,
            "consent": {"version": "health-test.v1", "accepted_at": "2026-10-16T12:00:00Z"},
            "diet_pattern": "VEGETARIAN",
            "restrictions": ["GLUTEN_FREE"],
            "allergies": ["PEANUTS", "SESAME"],
            "meals_per_day": 1 + version % 8,
            "hydration_liters_per_day": "2.5",
        }
    return {
        **common,
        "consent": {"version": "health-test.v1", "accepted_at": "2026-10-16T12:00:00Z"},
        "measured_at": "2026-10-16T07:30:00Z",
        "weight_kg": SYNTHETIC_WEIGHT,
        "height_cm": 172,
        "bmi": "23.2",
        "resting_heart_rate_bpm": 50 + version,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": SYNTHETIC_TEMPERATURE,
    }


def envelope(
    event_type: str, aggregate_id: UUID, version: int, payload: dict[str, Any], /, **overrides: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": "2026-10-16T12:00:00+00:00",
        "producer": "gestion-usuarios",
        "aggregate_id": str(aggregate_id),
        "aggregate_version": version,
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": payload,
    }
    values.update(overrides)
    return values


def profile_envelope(
    section: ProfileSection, user_id: UUID, version: int = 1, **overrides: Any
) -> dict[str, Any]:
    return envelope(
        EVENT_TYPE_BY_SECTION[section],
        user_id,
        version,
        _payload(section, user_id, version),
        **overrides,
    )


def registered_envelope(user_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    user_id = user_id or uuid4()
    payload = {
        "user_id": str(user_id),
        "full_name": "Ana Pérez",
        "email": f"ana-{uuid4().hex[:8]}@example.com",
        "actor_type": "athlete",
        "status": "active",
        "registered_at": "2026-10-16T12:00:00+00:00",
    }
    return envelope("usuario.registrado.v1", user_id, 1, payload, **overrides)
