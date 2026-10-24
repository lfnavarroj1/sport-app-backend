"""Unidades de trabajo SQLAlchemy de la base de escritura de entrenamientos (HU010, HU032,
HU034, HU014, HU026, HU028)."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.errors import (
    ActiveSessionExists,
    AuthorizationConflict,
    CalculationIdConflict,
    IdempotencyKeyConflict,
    SessionIdConflict,
)
from training_management.application.ports import CommandRecord, IdempotencyRecord, OutboxMessage
from training_management.domain.authorizations.model import (
    AuthorizationStatus,
    TrainingAuthorization,
)
from training_management.domain.metrics.model import (
    CalculationStatus,
    MetricInput,
    MetricResult,
    TrainingMetricCalculation,
)
from training_management.domain.nutrition.model import NutritionPlan
from training_management.domain.plans.model import PlanStatus, Sport, TrainingPlan
from training_management.domain.routes.model import (
    Difficulty,
    GeoPoint,
    Route,
    RouteStatus,
)
from training_management.domain.sessions.model import TrainingSession, TrainingSessionStatus
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
    AUTHORIZATION_PRIMARY_KEY,
    COMMAND_IDEMPOTENCY_PRIMARY_KEY,
    IDEMPOTENCY_PRIMARY_KEY,
    METRIC_CALCULATION_PRIMARY_KEY,
    TRAINING_SESSION_PRIMARY_KEY,
    CommandIdempotencyRow,
    IdempotencyKeyRow,
    NutritionPlanItemRow,
    NutritionPlanMealRow,
    NutritionPlanRow,
    OutboxEventRow,
    RouteRow,
    TrainingAuthorizationRow,
    TrainingMetricCalculationRow,
    TrainingMetricInputRow,
    TrainingPlanRow,
    TrainingPlanSessionRow,
    TrainingPlanTemplateRow,
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
