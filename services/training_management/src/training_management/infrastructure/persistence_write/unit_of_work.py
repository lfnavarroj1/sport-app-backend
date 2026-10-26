"""Unidades de trabajo SQLAlchemy de la base de escritura de entrenamientos (HU010, HU032,
HU034, HU014, HU026, HU028, HU037, HU036, HU043, HU042)."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import Select, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.errors import (
    ActiveSessionExists,
    AuthorizationConflict,
    CalculationIdConflict,
    DeviceConflict,
    IdempotencyKeyConflict,
    IntakeIdConflict,
    MonitoringSettingsConflict,
    ReportIdConflict,
    SessionIdConflict,
    SessionReminderPreferencesConflict,
)
from training_management.application.ports import (
    CommandRecord,
    IdempotencyRecord,
    MonitoringState,
    OutboxMessage,
)
from training_management.domain.authorizations.model import (
    AuthorizationStatus,
    TrainingAuthorization,
)
from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceProtocol,
    DeviceStatus,
    DeviceType,
)
from training_management.domain.health.model import (
    BodyArea,
    HealthReport,
    ReportStatus,
    ReportType,
    Severity,
)
from training_management.domain.measurements.model import (
    Measurement,
    MeasurementType,
    MeasurementUnit,
)
from training_management.domain.metrics.model import (
    CalculationStatus,
    MetricInput,
    MetricResult,
    TrainingMetricCalculation,
)
from training_management.domain.monitoring.model import MonitoringSettings, RiskAlert
from training_management.domain.nutrition.intake import FoodIntake, IntakeItem
from training_management.domain.nutrition.model import MealType, NutritionPlan
from training_management.domain.plans.model import PlanStatus, Sport, TrainingPlan
from training_management.domain.reminders.model import ReminderPreferences, ReminderSetting
from training_management.domain.routes.model import (
    Difficulty,
    GeoPoint,
    Route,
    RouteStatus,
)
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    NutritionRoutineTemplate,
    RoutineItem,
    RoutineMeal,
    RoutineStatus,
)
from training_management.domain.sessions.model import (
    SessionEndReason,
    TrainingSession,
    TrainingSessionStatus,
)
from training_management.domain.templates.model import (
    DayOfWeek,
    Goal,
    Level,
    TemplateStatus,
    TrainingPlanTemplate,
    WeeklySession,
)
from training_management.infrastructure.persistence_write.models import (
    ACTIVE_SESSION_INDEX,
    ATHLETE_DEVICE_PRIMARY_KEY,
    AUTHORIZATION_PRIMARY_KEY,
    COMMAND_IDEMPOTENCY_PRIMARY_KEY,
    FOOD_INTAKE_PRIMARY_KEY,
    HEALTH_REPORT_PRIMARY_KEY,
    IDEMPOTENCY_PRIMARY_KEY,
    METRIC_CALCULATION_PRIMARY_KEY,
    MONITORING_SETTINGS_PRIMARY_KEY,
    SESSION_REMINDER_PREFERENCE_PRIMARY_KEY,
    TRAINING_SESSION_PRIMARY_KEY,
    AthleteDeviceRow,
    CommandIdempotencyRow,
    FoodIntakeItemRow,
    FoodIntakeRow,
    HealthReportRow,
    IdempotencyKeyRow,
    MonitoringSettingsRow,
    NutritionPlanItemRow,
    NutritionPlanMealRow,
    NutritionPlanRow,
    NutritionRoutineTemplateRow,
    OutboxEventRow,
    RouteRow,
    SessionMonitoringStateRow,
    SessionReminderPreferenceRow,
    TrainingAuthorizationRow,
    TrainingMeasurementRow,
    TrainingMetricCalculationRow,
    TrainingMetricInputRow,
    TrainingPlanRow,
    TrainingPlanSessionRow,
    TrainingPlanTemplateRow,
    TrainingRiskAlertRow,
    TrainingSessionRow,
)


def _violated_constraint(error: IntegrityError) -> str | None:
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


class _SessionUnitOfWork:
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


class SqlAlchemyPlanningUnitOfWork(_SessionUnitOfWork):
    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        row = self.session.get(IdempotencyKeyRow, key)
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

    def has_active_authorization(self, athlete_id: UUID, professional_id: UUID) -> bool:
        status = self.session.scalar(
            select(TrainingAuthorizationRow.status)
            .where(
                TrainingAuthorizationRow.athlete_id == athlete_id,
                TrainingAuthorizationRow.professional_id == professional_id,
            )
            .with_for_update(read=True)
        )
        return status == AuthorizationStatus.ACTIVE.value

    def add_plan(self, plan: TrainingPlan) -> None:
        self.session.add(
            TrainingPlanRow(
                plan_id=plan.plan_id,
                athlete_id=plan.athlete_id,
                professional_id=plan.professional_id,
                title=plan.title,
                objectives=list(plan.objectives),
                start_date=plan.start_date,
                end_date=plan.end_date,
                status=plan.status.value,
                created_at=plan.created_at,
                updated_at=plan.created_at,
                version=plan.version,
            )
        )
        # El plan debe existir antes que sus sesiones (clave foránea local).
        self.session.flush()
        self.session.add_all(
            TrainingPlanSessionRow(
                plan_id=plan.plan_id,
                position=position,
                scheduled_date=session.scheduled_date,
                sport=session.sport.value,
                duration_minutes=session.duration_minutes,
                intensity_rpe=session.intensity_rpe,
            )
            for position, session in enumerate(plan.sessions)
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
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


def _authorization(row: TrainingAuthorizationRow) -> TrainingAuthorization:
    return TrainingAuthorization(
        athlete_id=row.athlete_id,
        professional_id=row.professional_id,
        status=AuthorizationStatus(row.status),
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
        version=row.version,
    )


class SqlAlchemyAuthorizationUnitOfWork(_SessionUnitOfWork):
    def get_for_update(
        self, athlete_id: UUID, professional_id: UUID
    ) -> TrainingAuthorization | None:
        row = self.session.scalar(
            select(TrainingAuthorizationRow)
            .where(
                TrainingAuthorizationRow.athlete_id == athlete_id,
                TrainingAuthorizationRow.professional_id == professional_id,
            )
            .with_for_update()
        )
        return _authorization(row) if row is not None else None

    def add(self, authorization: TrainingAuthorization) -> None:
        self.session.add(
            TrainingAuthorizationRow(
                athlete_id=authorization.athlete_id,
                professional_id=authorization.professional_id,
                status=authorization.status.value,
                granted_at=authorization.granted_at,
                revoked_at=authorization.revoked_at,
                updated_at=authorization.revoked_at or authorization.granted_at,
                version=authorization.version,
            )
        )

    def update(self, authorization: TrainingAuthorization) -> None:
        row = self.session.get(
            TrainingAuthorizationRow, (authorization.athlete_id, authorization.professional_id)
        )
        if row is None:
            raise RuntimeError("La autorización a actualizar no está cargada.")
        row.status = authorization.status.value
        row.granted_at = authorization.granted_at
        row.revoked_at = authorization.revoked_at
        row.updated_at = authorization.revoked_at or authorization.granted_at
        row.version = authorization.version

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == AUTHORIZATION_PRIMARY_KEY:
                raise AuthorizationConflict() from None
            raise


def _session(row: TrainingSessionRow) -> TrainingSession:
    return TrainingSession(
        session_id=row.session_id,
        athlete_id=row.athlete_id,
        plan_id=row.plan_id,
        sport=Sport(row.sport),
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_seconds=row.duration_seconds,
        status=TrainingSessionStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        end_reason=SessionEndReason(row.end_reason) if row.end_reason else None,
    )


_SESSION_CONFLICTS: dict[str | None, type[Exception]] = {
    COMMAND_IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    TRAINING_SESSION_PRIMARY_KEY: SessionIdConflict,
    ACTIVE_SESSION_INDEX: ActiveSessionExists,
}


class _CommandUnitOfWork(_SessionUnitOfWork):
    """Idempotencia de comandos por deportista y operación (`command_idempotency`)."""

    def find_command(self, actor_id: UUID, operation: str, key: UUID) -> CommandRecord | None:
        # Sin autoflush: los conflictos de la sesión deben aflorar en `commit`, donde se
        # traducen a errores de aplicación, y no en esta consulta.
        with self.session.no_autoflush:
            row = self.session.get(CommandIdempotencyRow, (actor_id, operation, key))
        if row is None:
            return None
        return CommandRecord(
            actor_id=row.actor_id,
            operation=row.operation,
            key=row.key,
            request_fingerprint=row.request_fingerprint,
            resource_id=row.resource_id,
            response=row.response,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    def delete_command(self, actor_id: UUID, operation: str, key: UUID) -> None:
        self.session.execute(
            delete(CommandIdempotencyRow).where(
                CommandIdempotencyRow.actor_id == actor_id,
                CommandIdempotencyRow.operation == operation,
                CommandIdempotencyRow.key == key,
            )
        )

    def add_command(self, record: CommandRecord) -> None:
        self.session.add(
            CommandIdempotencyRow(
                actor_id=record.actor_id,
                operation=record.operation,
                key=record.key,
                request_fingerprint=record.request_fingerprint,
                resource_id=record.resource_id,
                response=record.response,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )


class SqlAlchemySessionUnitOfWork(_CommandUnitOfWork):
    """HU032: sesión, outbox e idempotencia en una transacción de escritura."""

    def get_session(self, session_id: UUID, *, for_update: bool = False) -> TrainingSession | None:
        statement = select(TrainingSessionRow).where(TrainingSessionRow.session_id == session_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        return _session(row) if row is not None else None

    def plan_allows(self, plan_id: UUID, athlete_id: UUID, sport: Sport) -> bool:
        found = self.session.scalar(
            select(TrainingPlanRow.plan_id)
            .join(TrainingPlanSessionRow, TrainingPlanSessionRow.plan_id == TrainingPlanRow.plan_id)
            .where(
                TrainingPlanRow.plan_id == plan_id,
                TrainingPlanRow.athlete_id == athlete_id,
                TrainingPlanRow.status == PlanStatus.ASSIGNED.value,
                TrainingPlanSessionRow.sport == sport.value,
            )
            .limit(1)
        )
        return found is not None

    def add_session(self, training_session: TrainingSession) -> None:
        self.session.add(
            TrainingSessionRow(
                session_id=training_session.session_id,
                athlete_id=training_session.athlete_id,
                plan_id=training_session.plan_id,
                sport=training_session.sport.value,
                started_at=training_session.started_at,
                ended_at=training_session.ended_at,
                duration_seconds=training_session.duration_seconds,
                status=training_session.status.value,
                version=training_session.version,
                created_at=training_session.created_at,
                updated_at=training_session.updated_at,
            )
        )

    def update_session(self, training_session: TrainingSession) -> None:
        row = self.session.get(TrainingSessionRow, training_session.session_id)
        if row is None:
            raise RuntimeError("La sesión a actualizar no está cargada.")
        row.ended_at = training_session.ended_at
        row.duration_seconds = training_session.duration_seconds
        row.status = training_session.status.value
        row.version = training_session.version
        row.updated_at = training_session.updated_at
        row.end_reason = training_session.end_reason.value if training_session.end_reason else None

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _SESSION_CONFLICTS.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


def _calculation(
    row: TrainingMetricCalculationRow, inputs: list[TrainingMetricInputRow]
) -> TrainingMetricCalculation:
    return TrainingMetricCalculation(
        calculation_id=row.calculation_id,
        athlete_id=row.athlete_id,
        metric_code=row.metric_code,
        method_code=row.method_code,
        method_version=row.method_version,
        measured_at=row.measured_at,
        calculated_at=row.calculated_at,
        inputs=tuple(
            MetricInput(code=item.code, value=item.value, unit=item.unit) for item in inputs
        ),
        result=MetricResult(value=row.result_value, unit=row.result_unit, scale=row.result_scale),
        disclaimer_key=row.disclaimer_key,
        status=CalculationStatus(row.status),
        version=row.version,
    )


_METRIC_CONFLICTS: dict[str | None, type[Exception]] = {
    COMMAND_IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    METRIC_CALCULATION_PRIMARY_KEY: CalculationIdConflict,
}


class SqlAlchemyMetricUnitOfWork(_CommandUnitOfWork):
    """HU034: cálculo, entradas, outbox e idempotencia en una transacción de escritura."""

    def get_calculation(self, calculation_id: UUID) -> TrainingMetricCalculation | None:
        with self.session.no_autoflush:
            row = self.session.get(TrainingMetricCalculationRow, calculation_id)
            if row is None:
                return None
            inputs = list(
                self.session.scalars(
                    select(TrainingMetricInputRow)
                    .where(TrainingMetricInputRow.calculation_id == calculation_id)
                    .order_by(TrainingMetricInputRow.position)
                )
            )
        return _calculation(row, inputs)

    def add_calculation(self, calculation: TrainingMetricCalculation) -> None:
        self.session.add(
            TrainingMetricCalculationRow(
                calculation_id=calculation.calculation_id,
                athlete_id=calculation.athlete_id,
                metric_code=calculation.metric_code,
                method_code=calculation.method_code,
                method_version=calculation.method_version,
                measured_at=calculation.measured_at,
                calculated_at=calculation.calculated_at,
                result_value=calculation.result.value,
                result_unit=calculation.result.unit,
                result_scale=calculation.result.scale,
                status=calculation.status.value,
                disclaimer_key=calculation.disclaimer_key,
                version=calculation.version,
                data_fingerprint=calculation.data.fingerprint,
            )
        )
        self.session.add_all(
            TrainingMetricInputRow(
                calculation_id=calculation.calculation_id,
                code=item.code,
                position=position,
                value=item.value,
                unit=item.unit,
            )
            for position, item in enumerate(calculation.inputs)
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _METRIC_CONFLICTS.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


def _health_report(row: HealthReportRow) -> HealthReport:
    return HealthReport(
        report_id=row.report_id,
        athlete_id=row.athlete_id,
        type=ReportType(row.type),
        body_area=BodyArea(row.body_area),
        severity=Severity(row.severity),
        occurred_at=row.occurred_at,
        session_id=row.session_id,
        expected_recovery_days=row.expected_recovery_days,
        consent_version=row.health_consent_version,
        reported_at=row.reported_at,
        status=ReportStatus(row.status),
        version=row.version,
    )


_HEALTH_REPORT_CONFLICTS: dict[str | None, type[Exception]] = {
    COMMAND_IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    HEALTH_REPORT_PRIMARY_KEY: ReportIdConflict,
}


class SqlAlchemyHealthReportUnitOfWork(_CommandUnitOfWork):
    """HU042: reporte de salud, outbox e idempotencia en una transacción de escritura."""

    def get_session(self, session_id: UUID) -> TrainingSession | None:
        with self.session.no_autoflush:
            row = self.session.get(TrainingSessionRow, session_id)
        return _session(row) if row is not None else None

    def get_health_report(self, report_id: UUID) -> HealthReport | None:
        with self.session.no_autoflush:
            row = self.session.get(HealthReportRow, report_id)
        return _health_report(row) if row is not None else None

    def add_health_report(self, report: HealthReport) -> None:
        self.session.add(
            HealthReportRow(
                report_id=report.report_id,
                athlete_id=report.athlete_id,
                session_id=report.session_id,
                type=report.type.value,
                body_area=report.body_area.value,
                severity=report.severity.value,
                occurred_at=report.occurred_at,
                expected_recovery_days=report.expected_recovery_days,
                health_consent_version=report.consent_version,
                status=report.status.value,
                version=report.version,
                reported_at=report.reported_at,
            )
        )

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _HEALTH_REPORT_CONFLICTS.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


class SqlAlchemyNutritionUnitOfWork(SqlAlchemyPlanningUnitOfWork):
    """HU014: plan de alimentación, comidas, ítems, outbox e idempotencia (`idempotency_key`
    de HU010) en una transacción de escritura; misma autorización `FOR SHARE`."""

    def add_nutrition_plan(self, plan: NutritionPlan) -> None:
        self.session.add(
            NutritionPlanRow(
                plan_id=plan.plan_id,
                athlete_id=plan.athlete_id,
                professional_id=plan.professional_id,
                title=plan.title,
                objectives=list(plan.objectives),
                start_date=plan.start_date,
                end_date=plan.end_date,
                status=plan.status.value,
                catalog_version=plan.catalog_version,
                created_at=plan.created_at,
                updated_at=plan.created_at,
                version=plan.version,
            )
        )
        # El plan y sus comidas deben existir antes que los ítems (claves foráneas locales).
        self.session.flush()
        self.session.add_all(
            NutritionPlanMealRow(
                plan_id=plan.plan_id, position=position, meal_type=meal.meal_type.value
            )
            for position, meal in enumerate(plan.meals)
        )
        self.session.flush()
        self.session.add_all(
            NutritionPlanItemRow(
                plan_id=plan.plan_id,
                meal_position=meal_position,
                position=position,
                food_code=item.food_code,
                quantity=item.quantity,
                unit=item.unit,
            )
            for meal_position, meal in enumerate(plan.meals)
            for position, item in enumerate(meal.items)
        )


_FOOD_INTAKE_CONFLICTS: dict[str | None, type[Exception]] = {
    COMMAND_IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    FOOD_INTAKE_PRIMARY_KEY: IntakeIdConflict,
}


class SqlAlchemyFoodIntakeUnitOfWork(_CommandUnitOfWork):
    """HU043: consumo, ítems, outbox e idempotencia en una transacción de escritura."""

    def get_food_intake(self, intake_id: UUID) -> FoodIntake | None:
        with self.session.no_autoflush:
            row = self.session.get(FoodIntakeRow, intake_id)
            if row is None:
                return None
            items = list(
                self.session.scalars(
                    select(FoodIntakeItemRow)
                    .where(FoodIntakeItemRow.intake_id == intake_id)
                    .order_by(FoodIntakeItemRow.position)
                )
            )
        return FoodIntake(
            intake_id=row.intake_id,
            athlete_id=row.athlete_id,
            consumed_at=row.consumed_at,
            meal_type=MealType(row.meal_type),
            items=tuple(
                IntakeItem(food_code=item.food_code, quantity=item.quantity, unit=item.unit)
                for item in items
            ),
            catalog_version=row.catalog_version,
            registered_at=row.registered_at,
            version=row.version,
        )

    def add_food_intake(self, intake: FoodIntake) -> None:
        self.session.add(
            FoodIntakeRow(
                intake_id=intake.intake_id,
                athlete_id=intake.athlete_id,
                consumed_at=intake.consumed_at,
                meal_type=intake.meal_type.value,
                catalog_version=intake.catalog_version,
                registered_at=intake.registered_at,
                version=intake.version,
                data_fingerprint=intake.data.fingerprint,
            )
        )
        # El consumo debe existir antes que sus ítems (clave foránea local). Una carrera con el
        # mismo `intake_id` aflora en este `flush` y se traduce igual que en `commit`.
        self._translate(self.session.flush)
        self.session.add_all(
            FoodIntakeItemRow(
                intake_id=intake.intake_id,
                position=position,
                food_code=item.food_code,
                quantity=item.quantity,
                unit=item.unit,
            )
            for position, item in enumerate(intake.items)
        )

    def commit(self) -> None:
        self._translate(self.session.commit)

    def _translate(self, action: Callable[[], None]) -> None:
        try:
            action()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _FOOD_INTAKE_CONFLICTS.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


def _route(row: RouteRow) -> Route:
    return Route(
        route_id=row.route_id,
        organizer_id=row.organizer_id,
        name=row.name,
        description=row.description,
        sport=Sport(row.sport),
        distance_km=row.distance_km,
        difficulty=Difficulty(row.difficulty),
        city=row.city,
        country_code=row.country_code,
        start_point=GeoPoint(lat=row.start_lat, lng=row.start_lng),
        waypoints=tuple(GeoPoint(lat=item["lat"], lng=item["lng"]) for item in row.waypoints),
        status=RouteStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
        retired_at=row.retired_at,
    )


class SqlAlchemyRouteUnitOfWork(_CommandUnitOfWork):
    """HU026: ruta, outbox e idempotencia (`command_idempotency`) en una transacción."""

    def get_route_for_update(self, route_id: UUID) -> Route | None:
        row = self.session.scalar(
            select(RouteRow).where(RouteRow.route_id == route_id).with_for_update()
        )
        return _route(row) if row is not None else None

    def add_route(self, route: Route) -> None:
        self.session.add(
            RouteRow(
                route_id=route.route_id,
                organizer_id=route.organizer_id,
                name=route.name,
                description=route.description,
                sport=route.sport.value,
                distance_km=route.distance_km,
                difficulty=route.difficulty.value,
                city=route.city,
                country_code=route.country_code,
                start_lat=route.start_point.lat,
                start_lng=route.start_point.lng,
                waypoints=[{"lat": point.lat, "lng": point.lng} for point in route.waypoints],
                status=route.status.value,
                version=route.version,
                created_at=route.created_at,
                updated_at=route.updated_at,
                published_at=route.published_at,
                retired_at=route.retired_at,
            )
        )

    def update_route(self, route: Route) -> None:
        row = self.session.get(RouteRow, route.route_id)
        if row is None:
            raise RuntimeError("La ruta a actualizar no está cargada.")
        row.status = route.status.value
        row.version = route.version
        row.updated_at = route.updated_at
        row.retired_at = route.retired_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == COMMAND_IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


def _template(row: TrainingPlanTemplateRow) -> TrainingPlanTemplate:
    return TrainingPlanTemplate(
        template_id=row.template_id,
        professional_id=row.professional_id,
        title=row.title,
        objectives=row.objectives,
        sport=Sport(row.sport),
        goal=Goal(row.goal),
        level=Level(row.level),
        duration_weeks=row.duration_weeks,
        weekly_sessions=tuple(
            WeeklySession(
                day_of_week=DayOfWeek(item["day_of_week"]),
                description=item["description"],
                duration_minutes=item["duration_minutes"],
            )
            for item in row.weekly_sessions
        ),
        status=TemplateStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
        retired_at=row.retired_at,
    )


class SqlAlchemyTemplateUnitOfWork(_CommandUnitOfWork):
    """HU028: plantilla, outbox e idempotencia (`command_idempotency`) en una transacción."""

    def get_template_for_update(self, template_id: UUID) -> TrainingPlanTemplate | None:
        row = self.session.scalar(
            select(TrainingPlanTemplateRow)
            .where(TrainingPlanTemplateRow.template_id == template_id)
            .with_for_update()
        )
        return _template(row) if row is not None else None

    def add_template(self, template: TrainingPlanTemplate) -> None:
        self.session.add(
            TrainingPlanTemplateRow(
                template_id=template.template_id,
                professional_id=template.professional_id,
                title=template.title,
                objectives=template.objectives,
                sport=template.sport.value,
                goal=template.goal.value,
                level=template.level.value,
                duration_weeks=template.duration_weeks,
                weekly_sessions=[
                    {
                        "day_of_week": session.day_of_week.value,
                        "description": session.description,
                        "duration_minutes": session.duration_minutes,
                    }
                    for session in template.weekly_sessions
                ],
                status=template.status.value,
                version=template.version,
                created_at=template.created_at,
                updated_at=template.updated_at,
                published_at=template.published_at,
                retired_at=template.retired_at,
            )
        )

    def update_template(self, template: TrainingPlanTemplate) -> None:
        row = self.session.get(TrainingPlanTemplateRow, template.template_id)
        if row is None:
            raise RuntimeError("La plantilla a actualizar no está cargada.")
        row.status = template.status.value
        row.version = template.version
        row.updated_at = template.updated_at
        row.retired_at = template.retired_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == COMMAND_IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


def _routine(row: NutritionRoutineTemplateRow) -> NutritionRoutineTemplate:
    return NutritionRoutineTemplate(
        template_id=row.template_id,
        professional_id=row.professional_id,
        title=row.title,
        description=row.description,
        goal=Goal(row.goal),
        sport=Sport(row.sport) if row.sport is not None else None,
        dietary_pattern=DietPattern(row.dietary_pattern),
        contains_allergens=tuple(Allergy(item) for item in row.contains_allergens),
        duration_days=row.duration_days,
        daily_meals=tuple(
            RoutineMeal(
                meal=MealType(meal["meal"]),
                items=tuple(
                    RoutineItem(food_code=item["food_code"], portions=item["portions"])
                    for item in meal["items"]
                ),
            )
            for meal in row.daily_meals
        ),
        status=RoutineStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
        retired_at=row.retired_at,
    )


class SqlAlchemyRoutineUnitOfWork(_CommandUnitOfWork):
    """HU030: rutina, outbox e idempotencia (`command_idempotency`) en una transacción."""

    def get_routine_for_update(self, template_id: UUID) -> NutritionRoutineTemplate | None:
        row = self.session.scalar(
            select(NutritionRoutineTemplateRow)
            .where(NutritionRoutineTemplateRow.template_id == template_id)
            .with_for_update()
        )
        return _routine(row) if row is not None else None

    def add_routine(self, routine: NutritionRoutineTemplate) -> None:
        self.session.add(
            NutritionRoutineTemplateRow(
                template_id=routine.template_id,
                professional_id=routine.professional_id,
                title=routine.title,
                description=routine.description,
                goal=routine.goal.value,
                sport=routine.sport.value if routine.sport is not None else None,
                dietary_pattern=routine.dietary_pattern.value,
                contains_allergens=[allergen.value for allergen in routine.contains_allergens],
                duration_days=routine.duration_days,
                daily_meals=[
                    {
                        "meal": meal.meal.value,
                        "items": [
                            {"food_code": item.food_code, "portions": item.portions}
                            for item in meal.items
                        ],
                    }
                    for meal in routine.daily_meals
                ],
                status=routine.status.value,
                version=routine.version,
                created_at=routine.created_at,
                updated_at=routine.updated_at,
                published_at=routine.published_at,
                retired_at=routine.retired_at,
            )
        )

    def update_routine(self, routine: NutritionRoutineTemplate) -> None:
        row = self.session.get(NutritionRoutineTemplateRow, routine.template_id)
        if row is None:
            raise RuntimeError("La rutina a actualizar no está cargada.")
        row.status = routine.status.value
        row.version = routine.version
        row.updated_at = routine.updated_at
        row.retired_at = routine.retired_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == COMMAND_IDEMPOTENCY_PRIMARY_KEY:
                raise IdempotencyKeyConflict() from None
            raise


def _device(row: AthleteDeviceRow) -> AthleteDevice:
    return AthleteDevice(
        device_id=row.device_id,
        athlete_id=row.athlete_id,
        display_name=row.display_name,
        device_type=DeviceType(row.device_type),
        protocol=DeviceProtocol(row.protocol),
        status=DeviceStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        revoked_at=row.revoked_at,
    )


def _device_statement(athlete_id: UUID, device_id: str) -> Select[AthleteDeviceRow]:
    return select(AthleteDeviceRow).where(
        AthleteDeviceRow.athlete_id == athlete_id, AthleteDeviceRow.device_id == device_id
    )


_DEVICE_CONFLICTS: dict[str | None, type[Exception]] = {
    COMMAND_IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
    ATHLETE_DEVICE_PRIMARY_KEY: DeviceConflict,
}


class SqlAlchemyDeviceUnitOfWork(_CommandUnitOfWork):
    """HU037: vínculo del dispositivo e idempotencia en una transacción de escritura."""

    def get_device(
        self, athlete_id: UUID, device_id: str, *, for_update: bool = False
    ) -> AthleteDevice | None:
        statement = _device_statement(athlete_id, device_id)
        if for_update:
            statement = statement.with_for_update()
        with self.session.no_autoflush:
            row = self.session.scalar(statement)
        return _device(row) if row is not None else None

    def add_device(self, device: AthleteDevice) -> None:
        self.session.add(
            AthleteDeviceRow(
                athlete_id=device.athlete_id,
                device_id=device.device_id,
                display_name=device.display_name,
                device_type=device.device_type.value,
                protocol=device.protocol.value,
                status=device.status.value,
                version=device.version,
                created_at=device.created_at,
                updated_at=device.updated_at,
                revoked_at=device.revoked_at,
            )
        )

    def update_device(self, device: AthleteDevice) -> None:
        row = self.session.get(AthleteDeviceRow, (device.athlete_id, device.device_id))
        if row is None:
            raise RuntimeError("El dispositivo a actualizar no está cargado.")
        row.display_name = device.display_name
        row.device_type = device.device_type.value
        row.protocol = device.protocol.value
        row.status = device.status.value
        row.version = device.version
        row.updated_at = device.updated_at
        row.revoked_at = device.revoked_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            conflict = _DEVICE_CONFLICTS.get(_violated_constraint(error))
            if conflict is not None:
                raise conflict() from None
            raise


class SqlAlchemyDeviceReader:
    """HU037: dispositivos propios leídos de `athlete_device` (sin proyección, ADR-044)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(self, athlete_id: UUID) -> list[AthleteDevice]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AthleteDeviceRow)
                .where(AthleteDeviceRow.athlete_id == athlete_id)
                .order_by(AthleteDeviceRow.created_at.desc(), AthleteDeviceRow.device_id.desc())
            )
            return [_device(row) for row in rows]


class SqlAlchemyMeasurementUnitOfWork(_SessionUnitOfWork):
    """HU037/HU036: ingestión de un lote con la sesión y el dispositivo en `FOR SHARE` y el
    estado del monitoreo de la sesión en `FOR UPDATE`.

    Una finalización (HU032) o una revocación toman `FOR UPDATE` sobre la misma fila, así que
    esperan a que el lote confirme; un lote posterior ve el nuevo estado y se rechaza. Dos lotes
    de la misma sesión se evalúan en serie por el bloqueo de `session_monitoring_state`.
    """

    def get_session_for_ingestion(self, session_id: UUID) -> TrainingSession | None:
        row = self.session.scalar(
            select(TrainingSessionRow)
            .where(TrainingSessionRow.session_id == session_id)
            .with_for_update(read=True)
        )
        return _session(row) if row is not None else None

    def get_device_for_ingestion(self, athlete_id: UUID, device_id: str) -> AthleteDevice | None:
        row = self.session.scalar(
            _device_statement(athlete_id, device_id).with_for_update(read=True)
        )
        return _device(row) if row is not None else None

    def lock_monitoring_state(self, session_id: UUID, now: datetime) -> MonitoringState:
        self.session.execute(
            insert(SessionMonitoringStateRow)
            .values(session_id=session_id, summary_version=0, alert_count=0, updated_at=now)
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        row = self.session.scalar(
            select(SessionMonitoringStateRow)
            .where(SessionMonitoringStateRow.session_id == session_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("El estado del monitoreo no quedó registrado.")
        return MonitoringState(
            session_id=row.session_id,
            summary_version=row.summary_version,
            alert_count=row.alert_count,
        )

    def add_measurements(
        self, session_id: UUID, device_id: str, measurements: tuple[Measurement, ...]
    ) -> tuple[Measurement, ...]:
        statement = (
            insert(TrainingMeasurementRow)
            .values(
                [
                    {
                        "session_id": session_id,
                        "device_id": device_id,
                        "measured_at": item.measured_at,
                        "type": item.type.value,
                        "value": item.value,
                        "unit": item.unit.value,
                    }
                    for item in measurements
                ]
            )
            .on_conflict_do_nothing(index_elements=["session_id", "device_id", "measured_at"])
            .returning(TrainingMeasurementRow.measured_at, TrainingMeasurementRow.value)
        )
        return tuple(
            Measurement(
                measured_at=measured_at,
                type=MeasurementType.HEART_RATE,
                value=value,
                unit=MeasurementUnit.BPM,
            )
            for measured_at, value in self.session.execute(statement).all()
        )

    def latest_measured_at(self, session_id: UUID, device_id: str) -> datetime | None:
        latest: datetime | None = self.session.scalar(
            select(func.max(TrainingMeasurementRow.measured_at)).where(
                TrainingMeasurementRow.session_id == session_id,
                TrainingMeasurementRow.device_id == device_id,
            )
        )
        return latest

    def window_samples(
        self, session_id: UUID, device_id: str, since: datetime
    ) -> list[tuple[datetime, int]]:
        rows = self.session.execute(
            select(TrainingMeasurementRow.measured_at, TrainingMeasurementRow.value)
            .where(
                TrainingMeasurementRow.session_id == session_id,
                TrainingMeasurementRow.device_id == device_id,
                TrainingMeasurementRow.measured_at >= since,
            )
            .order_by(TrainingMeasurementRow.measured_at)
        ).all()
        return [(measured_at, value) for measured_at, value in rows]

    def alert_instants(self, session_id: UUID, since: datetime, until: datetime) -> list[datetime]:
        return list(
            self.session.scalars(
                select(TrainingRiskAlertRow.detected_at)
                .where(
                    TrainingRiskAlertRow.session_id == session_id,
                    TrainingRiskAlertRow.detected_at >= since,
                    TrainingRiskAlertRow.detected_at <= until,
                )
                .order_by(TrainingRiskAlertRow.detected_at)
            )
        )

    def add_alerts(
        self, alerts: tuple[RiskAlert, ...], first_sequence: int, created_at: datetime
    ) -> None:
        self.session.add_all(
            TrainingRiskAlertRow(
                alert_id=alert.alert_id,
                session_id=alert.session_id,
                device_id=alert.device_id,
                rule=alert.rule,
                severity=alert.severity.value,
                detected_at=alert.detected_at,
                threshold_bpm=alert.threshold_bpm,
                sequence=first_sequence + offset,
                created_at=created_at,
            )
            for offset, alert in enumerate(alerts)
        )

    def save_monitoring_state(self, state: MonitoringState, now: datetime) -> None:
        row = self.session.get(SessionMonitoringStateRow, state.session_id)
        if row is None:
            raise RuntimeError("El estado del monitoreo no está bloqueado.")
        row.summary_version = state.summary_version
        row.alert_count = state.alert_count
        row.updated_at = now

    def commit(self) -> None:
        self.session.commit()


def _settings(row: MonitoringSettingsRow) -> MonitoringSettings:
    return MonitoringSettings(
        athlete_id=row.athlete_id,
        max_heart_rate_alert_bpm=row.max_heart_rate_alert_bpm,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyMonitoringSettingsUnitOfWork(_SessionUnitOfWork):
    """HU036: umbral configurado por el deportista."""

    def get_settings(
        self, athlete_id: UUID, *, for_update: bool = False
    ) -> MonitoringSettings | None:
        statement = select(MonitoringSettingsRow).where(
            MonitoringSettingsRow.athlete_id == athlete_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        return _settings(row) if row is not None else None

    def add_settings(self, settings: MonitoringSettings) -> None:
        self.session.add(
            MonitoringSettingsRow(
                athlete_id=settings.athlete_id,
                max_heart_rate_alert_bpm=settings.max_heart_rate_alert_bpm,
                version=settings.version,
                created_at=settings.created_at,
                updated_at=settings.updated_at,
            )
        )

    def update_settings(self, settings: MonitoringSettings) -> None:
        row = self.session.get(MonitoringSettingsRow, settings.athlete_id)
        if row is None:
            raise RuntimeError("La configuración a actualizar no está cargada.")
        row.max_heart_rate_alert_bpm = settings.max_heart_rate_alert_bpm
        row.version = settings.version
        row.updated_at = settings.updated_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == MONITORING_SETTINGS_PRIMARY_KEY:
                raise MonitoringSettingsConflict() from None
            raise


def _preferences(row: SessionReminderPreferenceRow) -> ReminderPreferences:
    return ReminderPreferences(
        athlete_id=row.athlete_id,
        hydration=ReminderSetting(row.hydration_enabled, row.hydration_interval_minutes),
        nutrition=ReminderSetting(row.nutrition_enabled, row.nutrition_interval_minutes),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemySessionReminderPreferencesUnitOfWork(_SessionUnitOfWork):
    """HU038: preferencias de recordatorios en sesión del deportista."""

    def get_preferences(
        self, athlete_id: UUID, *, for_update: bool = False
    ) -> ReminderPreferences | None:
        statement = select(SessionReminderPreferenceRow).where(
            SessionReminderPreferenceRow.athlete_id == athlete_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        return _preferences(row) if row is not None else None

    def add_preferences(self, preferences: ReminderPreferences) -> None:
        if preferences.created_at is None or preferences.updated_at is None:
            raise RuntimeError("Las preferencias a guardar no tienen fechas.")
        self.session.add(
            SessionReminderPreferenceRow(
                athlete_id=preferences.athlete_id,
                hydration_enabled=preferences.hydration.enabled,
                hydration_interval_minutes=preferences.hydration.interval_minutes,
                nutrition_enabled=preferences.nutrition.enabled,
                nutrition_interval_minutes=preferences.nutrition.interval_minutes,
                version=preferences.version,
                created_at=preferences.created_at,
                updated_at=preferences.updated_at,
            )
        )

    def update_preferences(self, preferences: ReminderPreferences) -> None:
        row = self.session.get(SessionReminderPreferenceRow, preferences.athlete_id)
        if row is None or preferences.updated_at is None:
            raise RuntimeError("Las preferencias a actualizar no están cargadas.")
        row.hydration_enabled = preferences.hydration.enabled
        row.hydration_interval_minutes = preferences.hydration.interval_minutes
        row.nutrition_enabled = preferences.nutrition.enabled
        row.nutrition_interval_minutes = preferences.nutrition.interval_minutes
        row.version = preferences.version
        row.updated_at = preferences.updated_at

    def commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            if _violated_constraint(error) == SESSION_REMINDER_PREFERENCE_PRIMARY_KEY:
                raise SessionReminderPreferencesConflict() from None
            raise


class SqlAlchemyMeasurementRetention:
    """HU036: borra por lotes las muestras con más de 90 días (ADR-044 §7)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def purge_measurements(self, older_than: datetime, batch_size: int) -> int:
        expired = (
            select(
                TrainingMeasurementRow.session_id,
                TrainingMeasurementRow.device_id,
                TrainingMeasurementRow.measured_at,
            )
            .where(TrainingMeasurementRow.measured_at < older_than)
            .order_by(TrainingMeasurementRow.measured_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        with self._session_factory() as session, session.begin():
            keys = session.execute(expired).all()
            if not keys:
                return 0
            session.execute(
                delete(TrainingMeasurementRow).where(
                    tuple_(
                        TrainingMeasurementRow.session_id,
                        TrainingMeasurementRow.device_id,
                        TrainingMeasurementRow.measured_at,
                    ).in_([tuple(key) for key in keys])
                )
            )
        return len(keys)
