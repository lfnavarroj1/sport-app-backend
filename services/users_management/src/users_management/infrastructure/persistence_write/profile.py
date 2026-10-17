"""Unidad de trabajo SQLAlchemy de los comandos de perfil (HU005, ADR-034).

La sección, `outbox_event` e `idempotency_key` se confirman en una transacción. El
documento de la sección (respuesta y payload) se traduce a columnas tipadas y de vuelta
con los mismos formatos: decimales con un decimal e instantes en UTC con sufijo `Z`.
"""

from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Select, delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from users_management.application.errors import IdempotencyKeyConflict, SectionAlreadyExists
from users_management.application.ports import (
    IdempotencyRecord,
    OutboxMessage,
    StoredProfileSection,
)
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.validation import format_decimal, format_instant
from users_management.infrastructure.persistence_write.models import (
    DEMOGRAPHIC_PRIMARY_KEY,
    IDEMPOTENCY_PRIMARY_KEY,
    PHYSIOLOGICAL_PRIMARY_KEY,
    DemographicProfileRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    PhysiologicalProfileRow,
)

SectionRow = DemographicProfileRow | PhysiologicalProfileRow
_ROW_BY_SECTION: dict[ProfileSection, type[SectionRow]] = {
    ProfileSection.DEMOGRAPHIC: DemographicProfileRow,
    ProfileSection.PHYSIOLOGICAL: PhysiologicalProfileRow,
}


class ProfileWriteFailed(RuntimeError):
    """Fallo de PostgreSQL al escribir una sección.

    El mensaje de SQLAlchemy incluye la sentencia y sus parámetros (datos demográficos y
    de salud) y PostgreSQL la fila rechazada; se descartan para que nunca lleguen a los
    logs. Solo se conservan la clase del error y la restricción incumplida.
    """

    def __init__(self, error: DBAPIError) -> None:
        constraint = _violated_constraint(error)
        super().__init__(f"{type(error.orig).__name__} constraint={constraint}")


def _violated_constraint(error: DBAPIError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _optional_decimal(value: Decimal | None) -> str | None:
    return format_decimal(value) if value is not None else None


def demographic_document(row: DemographicProfileRow) -> dict[str, Any]:
    return {
        "user_id": str(row.user_id),
        "version": row.version,
        "birth_date": row.birth_date.isoformat(),
        "gender": row.gender,
        "country_code": row.country_code,
        "city": row.city,
        "marital_status": row.marital_status,
        "occupation": row.occupation,
        "income_range": row.income_range,
        "created_at": format_instant(row.created_at),
        "updated_at": format_instant(row.updated_at),
    }


def physiological_document(row: PhysiologicalProfileRow) -> dict[str, Any]:
    pressure = None
    if row.systolic_mmhg is not None and row.diastolic_mmhg is not None:
        pressure = {"systolic_mmhg": row.systolic_mmhg, "diastolic_mmhg": row.diastolic_mmhg}
    return {
        "user_id": str(row.user_id),
        "version": row.version,
        "consent": {
            "version": row.health_consent_version,
            "accepted_at": format_instant(row.health_consent_at),
        },
        "measured_at": format_instant(row.measured_at),
        "weight_kg": format_decimal(row.weight_kg),
        "height_cm": row.height_cm,
        "bmi": format_decimal(row.bmi),
        "resting_heart_rate_bpm": row.resting_heart_rate_bpm,
        "blood_pressure": pressure,
        "body_temperature_c": _optional_decimal(row.body_temperature_c),
        "created_at": format_instant(row.created_at),
        "updated_at": format_instant(row.updated_at),
    }


def _demographic_columns(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": document["version"],
        "birth_date": date.fromisoformat(document["birth_date"]),
        "gender": document["gender"],
        "country_code": document["country_code"],
        "city": document["city"],
        "marital_status": document["marital_status"],
        "occupation": document["occupation"],
        "income_range": document["income_range"],
        "created_at": datetime.fromisoformat(document["created_at"]),
        "updated_at": datetime.fromisoformat(document["updated_at"]),
    }


def _physiological_columns(document: dict[str, Any]) -> dict[str, Any]:
    pressure = document["blood_pressure"] or {}
    temperature = document["body_temperature_c"]
    return {
        "version": document["version"],
        "health_consent_version": document["consent"]["version"],
        "health_consent_at": datetime.fromisoformat(document["consent"]["accepted_at"]),
        "measured_at": datetime.fromisoformat(document["measured_at"]),
        "weight_kg": Decimal(document["weight_kg"]),
        "height_cm": document["height_cm"],
        "bmi": Decimal(document["bmi"]),
        "resting_heart_rate_bpm": document["resting_heart_rate_bpm"],
        "systolic_mmhg": pressure.get("systolic_mmhg"),
        "diastolic_mmhg": pressure.get("diastolic_mmhg"),
        "body_temperature_c": Decimal(temperature) if temperature is not None else None,
        "created_at": datetime.fromisoformat(document["created_at"]),
        "updated_at": datetime.fromisoformat(document["updated_at"]),
    }


def section_document(row: SectionRow) -> dict[str, Any]:
    if isinstance(row, DemographicProfileRow):
        return demographic_document(row)
    return physiological_document(row)


def _section_columns(section: ProfileSection, document: dict[str, Any]) -> dict[str, Any]:
    if section is ProfileSection.DEMOGRAPHIC:
        return _demographic_columns(document)
    return _physiological_columns(document)


def _locked[RowT: (DemographicProfileRow, PhysiologicalProfileRow)](
    model: type[RowT], user_id: UUID
) -> Select[RowT]:
    return (
        select(model)
        .where(model.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class SqlAlchemyProfileUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("La unidad de trabajo no está abierta.")
        return self._session

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Todo lo no confirmado explícitamente se descarta.
        self.session.rollback()
        self.session.close()
        self._session = None

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        # Sin *autoflush*: la consulta no debe adelantar al `SELECT` un conflicto que
        # solo se traduce en `commit` (lección de HU032).
        with self.session.no_autoflush:
            row = self.session.scalar(
                select(IdempotencyKeyRow)
                .where(IdempotencyKeyRow.key == key)
                .execution_options(populate_existing=True)
            )
        if row is None:
            return None
        return IdempotencyRecord(
            key=row.key,
            operation=row.operation,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_idempotency(self, key: UUID) -> None:
        self.session.execute(delete(IdempotencyKeyRow).where(IdempotencyKeyRow.key == key))

    def lock_section(self, section: ProfileSection, user_id: UUID) -> StoredProfileSection | None:
        row: SectionRow | None
        if section is ProfileSection.DEMOGRAPHIC:
            row = self.session.scalar(_locked(DemographicProfileRow, user_id))
        else:
            row = self.session.scalar(_locked(PhysiologicalProfileRow, user_id))
        if row is None:
            return None
        return StoredProfileSection(
            section=section,
            user_id=row.user_id,
            version=row.version,
            document=section_document(row),
        )

    def save_section(self, stored: StoredProfileSection, *, created: bool) -> None:
        model = _ROW_BY_SECTION[stored.section]
        columns = _section_columns(stored.section, stored.document)
        if created:
            self.session.add(model(user_id=stored.user_id, **columns))
            return
        try:
            self.session.execute(
                update(model).where(model.user_id == stored.user_id).values(**columns),
                execution_options={"synchronize_session": False},
            )
        except DBAPIError as error:
            raise ProfileWriteFailed(error) from None

    def add_outbox(self, message: OutboxMessage) -> None:
        self.session.add(
            OutboxEventRow(
                event_id=message.event_id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                aggregate_version=message.aggregate_version,
                event_type=message.event_type,
                schema_version=message.schema_version,
                occurred_at=message.occurred_at,
                correlation_id=message.correlation_id,
                causation_id=message.causation_id,
                payload=message.payload,
                attempts=0,
                next_attempt_at=message.occurred_at,
            )
        )

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self.session.add(
            IdempotencyKeyRow(
                key=record.key,
                operation=record.operation,
                request_fingerprint=record.request_fingerprint,
                resource_id=record.resource_id,
                response=record.response,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except DBAPIError as error:
            self.session.rollback()
            constraint = _violated_constraint(error)
            if constraint in (DEMOGRAPHIC_PRIMARY_KEY, PHYSIOLOGICAL_PRIMARY_KEY):
                raise SectionAlreadyExists() from None
            if constraint == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise ProfileWriteFailed(error) from None
