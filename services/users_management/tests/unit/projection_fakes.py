"""Eventos sintéticos de `sportapp.usuarios.v1` y base de lectura en memoria para probar
`usuarios-read-projector` sin infraestructura."""

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from users_management.application.projections.users_projection import DirectoryEntry
from users_management.domain.profile.events import EVENT_TYPE_BY_SECTION, ProfileSection

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
# Valores sintéticos fáciles de buscar en los logs capturados (no deben aparecer nunca).
SYNTHETIC_WEIGHT = "68.5"
SYNTHETIC_TEMPERATURE = "36.6"
SYNTHETIC_CITY = "Medellín"


def demographic_payload(user_id: UUID, version: int = 1, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": str(user_id),
        "version": version,
        "birth_date": "1990-05-14",
        "gender": "FEMALE",
        "country_code": "CO",
        "city": SYNTHETIC_CITY,
        "marital_status": None,
        "occupation": "Ingeniera",
        "income_range": "2_4_SMMLV",
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
    }
    values.update(overrides)
    return values


def physiological_payload(user_id: UUID, version: int = 1, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": str(user_id),
        "version": version,
        "consent": {"version": "health-test.v1", "accepted_at": "2026-10-16T12:00:00.123456Z"},
        "measured_at": "2026-10-16T07:30:00Z",
        "weight_kg": SYNTHETIC_WEIGHT,
        "height_cm": 172,
        "bmi": "23.2",
        "resting_heart_rate_bpm": 58,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": SYNTHETIC_TEMPERATURE,
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
    }
    values.update(overrides)
    return values


def sport_habits_payload(user_id: UUID, version: int = 1, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": str(user_id),
        "version": version,
        "sports": [
            {
                "sport": "running",
                "frequency_per_week": 3,
                "session_duration_minutes": 45,
                "intensity": "MODERATE",
                "level": "INTERMEDIATE",
                "years_practicing": 4,
            }
        ],
        "availability": {"days": ["MON", "WED", "SAT"], "time_slots": ["MORNING"]},
        "primary_goal": "HEALTH",
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
    }
    values.update(overrides)
    return values


def nutrition_habits_payload(user_id: UUID, version: int = 1, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": str(user_id),
        "version": version,
        "consent": {"version": "health-test.v1", "accepted_at": "2026-10-24T12:00:00Z"},
        "diet_pattern": "VEGETARIAN",
        "restrictions": ["GLUTEN_FREE", "LOW_SUGAR"],
        "allergies": ["PEANUTS", "SESAME"],
        "meals_per_day": 4,
        "hydration_liters_per_day": "2.5",
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
    }
    values.update(overrides)
    return values


PAYLOAD_BY_SECTION = {
    ProfileSection.DEMOGRAPHIC: demographic_payload,
    ProfileSection.PHYSIOLOGICAL: physiological_payload,
    ProfileSection.SPORT_HABITS: sport_habits_payload,
    ProfileSection.NUTRITION_HABITS: nutrition_habits_payload,
}


def envelope(
    event_type: str,
    aggregate_id: UUID,
    version: int,
    payload: dict[str, Any],
    /,
    **overrides: Any,
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
    section: ProfileSection,
    user_id: UUID | None = None,
    version: int = 1,
    *,
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    user_id = user_id or uuid4()
    body = payload if payload is not None else PAYLOAD_BY_SECTION[section](user_id, version)
    return envelope(EVENT_TYPE_BY_SECTION[section], user_id, version, body, **overrides)


def registered_envelope(
    user_id: UUID | None = None,
    *,
    email: str | None = None,
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    user_id = user_id or uuid4()
    body = payload or {
        "user_id": str(user_id),
        "full_name": "Ana Pérez",
        "email": email or f"ana-{uuid4().hex[:8]}@example.com",
        "actor_type": "athlete",
        "status": "active",
        "registered_at": "2026-10-16T12:00:00+00:00",
    }
    return envelope("usuario.registrado.v1", user_id, 1, body, **overrides)


class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass
class InMemoryReadStore:
    processed: dict[UUID, tuple[str, UUID, int]] = field(default_factory=dict)
    directory: dict[UUID, DirectoryEntry] = field(default_factory=dict)
    profiles: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    commits: int = 0


class InMemoryProjectionUnitOfWork:
    """Copia el estado al abrir y lo publica solo con `commit` (todo o nada)."""

    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store
        self._draft: InMemoryReadStore | None = None

    @property
    def draft(self) -> InMemoryReadStore:
        if self._draft is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._draft

    def __enter__(self) -> Self:
        self._draft = copy.deepcopy(self.store)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._draft = None

    def claim_event(
        self,
        event_id: UUID,
        projection_name: str,
        aggregate_id: UUID,
        aggregate_version: int,
        processed_at: datetime,
    ) -> bool:
        if event_id in self.draft.processed:
            return False
        self.draft.processed[event_id] = (projection_name, aggregate_id, aggregate_version)
        return True

    def directory_version(self, user_id: UUID) -> int | None:
        entry = self.draft.directory.get(user_id)
        return entry.aggregate_version if entry is not None else None

    def add_directory_entry(self, entry: DirectoryEntry, projected_at: datetime) -> bool:
        if entry.user_id in self.draft.directory:
            return False
        self.draft.directory[entry.user_id] = entry
        return True

    def profile_section_version(self, user_id: UUID, section: ProfileSection) -> int | None:
        row = self.draft.profiles.get(user_id)
        return row.get(f"{section.value}_version") if row is not None else None

    def save_profile_section(
        self,
        user_id: UUID,
        section: ProfileSection,
        version: int,
        document: dict[str, Any],
        projected_at: datetime,
    ) -> bool:
        row = self.draft.profiles.setdefault(user_id, {})
        if (row.get(f"{section.value}_version") or 0) >= version:
            return False
        row.update(
            {
                section.value: copy.deepcopy(document),
                f"{section.value}_version": version,
                "projected_at": projected_at,
            }
        )
        return True

    def commit(self) -> None:
        draft = self.draft
        self.store.processed = draft.processed
        self.store.directory = draft.directory
        self.store.profiles = draft.profiles
        self.store.commits += 1
