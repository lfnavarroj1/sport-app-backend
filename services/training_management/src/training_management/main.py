"""Aplicación FastAPI de `training_management`."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier
from training_management.api.authorizations import build_authorizations_router
from training_management.api.authorized_athletes import build_authorized_athletes_router
from training_management.api.devices import build_devices_router
from training_management.api.food_catalog import build_food_catalog_router
from training_management.api.food_intakes import build_food_intakes_router
from training_management.api.health_history import build_health_history_router
from training_management.api.health_reports import build_health_reports_router
from training_management.api.history import build_history_router
from training_management.api.measurements import build_measurements_router
from training_management.api.monitoring_settings import build_monitoring_settings_router
from training_management.api.nutrition_plans import build_nutrition_plans_router
from training_management.api.planned_sessions import build_planned_sessions_router
from training_management.api.plans import build_plans_router
from training_management.api.routes import build_routes_router
from training_management.api.session_reminders import build_session_reminders_router
from training_management.api.training_dashboard import build_training_dashboard_router
from training_management.api.training_metrics import build_training_metrics_router
from training_management.api.training_plan_templates import (
    build_training_plan_templates_router,
)
from training_management.api.training_sessions import build_training_sessions_router
from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.commands.calculate_training_metric import (
    CalculateTrainingMetric,
)
from training_management.application.commands.create_nutrition_plan import CreateNutritionPlan
from training_management.application.commands.create_training_plan import CreateTrainingPlan
from training_management.application.commands.devices import RegisterDevice, RevokeDevice
from training_management.application.commands.measurements import IngestMeasurements
from training_management.application.commands.monitoring_settings import (
    UpdateMonitoringSettings,
)
from training_management.application.commands.register_food_intake import RegisterFoodIntake
from training_management.application.commands.report_health import ReportHealth
from training_management.application.commands.routes import PublishRoute, RetireRoute
from training_management.application.commands.session_reminder_preferences import (
    UpdateSessionReminderPreferences,
)
from training_management.application.commands.training_plan_templates import (
    PublishTemplate,
    RetireTemplate,
)
from training_management.application.commands.training_sessions import (
    FinishTrainingSession,
    StartTrainingSession,
)
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.application.queries.active_session import GetActiveTrainingSession
from training_management.application.queries.authorized_athletes import GetAuthorizedAthletes
from training_management.application.queries.devices import ListMyDevices
from training_management.application.queries.food_catalog import GetFoodCatalog
from training_management.application.queries.get_training_plan import GetTrainingPlan
from training_management.application.queries.health_history import GetHealthHistory
from training_management.application.queries.monitoring_settings import GetMonitoringSettings
from training_management.application.queries.nutrition_plans import (
    GetNutritionPlan,
    ListMyNutritionPlans,
)
from training_management.application.queries.planned_sessions import GetPlannedSessions
from training_management.application.queries.routes import ListMyRoutes, SuggestRoutes
from training_management.application.queries.session_reminders import (
    GetSessionReminderPreferences,
    GetSessionReminderRules,
)
from training_management.application.queries.training_dashboard import GetTrainingDashboard
from training_management.application.queries.training_history import GetTrainingHistory
from training_management.application.queries.training_metrics import (
    GetMetricCatalog,
    ListTrainingMetricResults,
)
from training_management.application.queries.training_plan_templates import (
    ListMyTemplates,
    SuggestTemplates,
)
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG
from training_management.infrastructure.authorization import ContractPermissionPolicy
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.integrations.affiliation import (
    AffiliationCapabilityChecker,
)
from training_management.infrastructure.integrations.users import (
    UsersAthleteBirthDates,
    UsersNutritionRestrictions,
)
from training_management.infrastructure.persistence_read.dashboard import SqlAlchemyDashboardReader
from training_management.infrastructure.persistence_read.health_history import (
    SqlAlchemyHealthReportHistoryReader,
)
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyActiveSessionReader,
    SqlAlchemyAuthorizationReader,
    SqlAlchemyAuthorizedAthleteReader,
    SqlAlchemyHistoryReader,
    SqlAlchemyPlannedSessionReader,
)
from training_management.infrastructure.persistence_read.metrics import (
    SqlAlchemyMetricResultReader,
)
from training_management.infrastructure.persistence_read.nutrition import (
    SqlAlchemyNutritionPlanReader,
)
from training_management.infrastructure.persistence_read.plans import SqlAlchemyPlanReader
from training_management.infrastructure.persistence_read.routes import SqlAlchemyRouteReader
from training_management.infrastructure.persistence_read.templates import SqlAlchemyTemplateReader
from training_management.infrastructure.persistence_write.food_catalog import (
    FoodCatalogReadinessCheck,
    SqlAlchemyFoodCatalogReader,
)
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyAuthorizationUnitOfWork,
    SqlAlchemyDeviceReader,
    SqlAlchemyDeviceUnitOfWork,
    SqlAlchemyFoodIntakeUnitOfWork,
    SqlAlchemyHealthReportUnitOfWork,
    SqlAlchemyMeasurementUnitOfWork,
    SqlAlchemyMetricUnitOfWork,
    SqlAlchemyMonitoringSettingsUnitOfWork,
    SqlAlchemyNutritionUnitOfWork,
    SqlAlchemyPlanningUnitOfWork,
    SqlAlchemyRouteUnitOfWork,
    SqlAlchemySessionReminderPreferencesUnitOfWork,
    SqlAlchemySessionUnitOfWork,
    SqlAlchemyTemplateUnitOfWork,
)
from training_management.settings import TrainingManagementSettings

SERVICE_NAME = "training-management"
API_VERSION = "0.19.0"


def create_app(
    settings: TrainingManagementSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` y `affiliation_http` permiten inyectar transportes en pruebas de componente."""
    settings = settings or load_settings(TrainingManagementSettings)
    permissions = ContractPermissionPolicy(
        load_actor_permissions(settings.actor_permissions_path or resolve_actor_permissions_path())
    )
    engine = create_database_engine(settings.database_write_url, settings)
    read_engine = create_database_engine(settings.database_read_url, settings)
    writes = sessionmaker(engine, expire_on_commit=False)
    reads = sessionmaker(read_engine, expire_on_commit=False)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    affiliation_http = affiliation_http or httpx.Client(
        base_url=str(settings.affiliation_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )

    def fetch_jwks() -> dict[str, Any]:
        response = users_http.get("/.well-known/jwks.json")
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document

    verifier = CachedJwksVerifier(
        fetch_jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    clock = SystemClock()
    capabilities = AffiliationCapabilityChecker(affiliation_http)
    food_catalog = SqlAlchemyFoodCatalogReader(writes)
    authorization_views = SqlAlchemyAuthorizationReader(reads)
    route_views = SqlAlchemyRouteReader(reads)
    template_views = SqlAlchemyTemplateReader(reads)
    thresholds = ResolveThreshold(
        unit_of_work=lambda: SqlAlchemyMonitoringSettingsUnitOfWork(writes),
        birth_dates=UsersAthleteBirthDates(users_http),
        clock=clock,
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Entrenamientos",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada. HU014: el
        # catálogo de alimentos de la base de escritura debe estar completo en su versión.
        readiness_checks=[
            DatabaseReadinessCheck("database_write", engine),
            FoodCatalogReadinessCheck(engine),
        ],
        routers=[
            build_authorizations_router(
                verify_token=verifier.verify,
                grant=GrantAuthorization(
                    unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
                revoke=RevokeAuthorization(
                    unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(writes), clock=clock
                ),
            ),
            build_plans_router(
                verify_token=verifier.verify,
                create_plan=CreateTrainingPlan(
                    unit_of_work=lambda: SqlAlchemyPlanningUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                get_plan=GetTrainingPlan(
                    reader=SqlAlchemyPlanReader(reads),
                    permissions=permissions,
                    capabilities=capabilities,
                ),
            ),
            build_history_router(
                verify_token=verifier.verify,
                get_history=GetTrainingHistory(
                    reader=SqlAlchemyHistoryReader(reads),
                    authorizations=authorization_views,
                    permissions=permissions,
                ),
            ),
            build_authorized_athletes_router(
                verify_token=verifier.verify,
                get_authorized_athletes=GetAuthorizedAthletes(
                    reader=SqlAlchemyAuthorizedAthleteReader(reads), permissions=permissions
                ),
            ),
            build_training_sessions_router(
                verify_token=verifier.verify,
                start=StartTrainingSession(
                    unit_of_work=lambda: SqlAlchemySessionUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
                finish=FinishTrainingSession(
                    unit_of_work=lambda: SqlAlchemySessionUnitOfWork(writes), clock=clock
                ),
                get_active=GetActiveTrainingSession(reader=SqlAlchemyActiveSessionReader(reads)),
            ),
            build_planned_sessions_router(
                verify_token=verifier.verify,
                get_sessions=GetPlannedSessions(reader=SqlAlchemyPlannedSessionReader(reads)),
            ),
            build_training_metrics_router(
                verify_token=verifier.verify,
                get_catalog=GetMetricCatalog(
                    capabilities=capabilities, catalog=TRAINING_METRICS_CATALOG
                ),
                calculate=CalculateTrainingMetric(
                    unit_of_work=lambda: SqlAlchemyMetricUnitOfWork(writes),
                    capabilities=capabilities,
                    catalog=TRAINING_METRICS_CATALOG,
                    clock=clock,
                ),
                list_results=ListTrainingMetricResults(
                    reader=SqlAlchemyMetricResultReader(reads),
                    capabilities=capabilities,
                    catalog=TRAINING_METRICS_CATALOG,
                ),
            ),
            build_health_reports_router(
                verify_token=verifier.verify,
                report_health=ReportHealth(
                    unit_of_work=lambda: SqlAlchemyHealthReportUnitOfWork(writes),
                    clock=clock,
                    consent_version=settings.health_data_consent_version,
                ),
            ),
            build_health_history_router(
                verify_token=verifier.verify,
                get_health_history=GetHealthHistory(
                    reader=SqlAlchemyHealthReportHistoryReader(reads),
                    authorizations=authorization_views,
                    permissions=permissions,
                ),
            ),
            build_training_dashboard_router(
                verify_token=verifier.verify,
                get_dashboard=GetTrainingDashboard(
                    reader=SqlAlchemyDashboardReader(reads), capabilities=capabilities, clock=clock
                ),
            ),
            build_nutrition_plans_router(
                verify_token=verifier.verify,
                create_plan=CreateNutritionPlan(
                    unit_of_work=lambda: SqlAlchemyNutritionUnitOfWork(writes),
                    permissions=permissions,
                    catalog=food_catalog,
                    restrictions=UsersNutritionRestrictions(users_http),
                    clock=clock,
                ),
                get_plan=GetNutritionPlan(
                    reader=SqlAlchemyNutritionPlanReader(reads),
                    authorizations=authorization_views,
                    permissions=permissions,
                ),
                list_plans=ListMyNutritionPlans(reader=SqlAlchemyNutritionPlanReader(reads)),
            ),
            build_food_catalog_router(
                verify_token=verifier.verify, get_catalog=GetFoodCatalog(catalog=food_catalog)
            ),
            build_food_intakes_router(
                verify_token=verifier.verify,
                register=RegisterFoodIntake(
                    unit_of_work=lambda: SqlAlchemyFoodIntakeUnitOfWork(writes),
                    catalog=food_catalog,
                    clock=clock,
                ),
            ),
            build_routes_router(
                verify_token=verifier.verify,
                publish=PublishRoute(
                    unit_of_work=lambda: SqlAlchemyRouteUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                retire=RetireRoute(
                    unit_of_work=lambda: SqlAlchemyRouteUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                list_routes=ListMyRoutes(reader=route_views, permissions=permissions),
                suggest=SuggestRoutes(reader=route_views, capabilities=capabilities),
            ),
            build_training_plan_templates_router(
                verify_token=verifier.verify,
                publish=PublishTemplate(
                    unit_of_work=lambda: SqlAlchemyTemplateUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                retire=RetireTemplate(
                    unit_of_work=lambda: SqlAlchemyTemplateUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                list_templates=ListMyTemplates(reader=template_views, permissions=permissions),
                suggest=SuggestTemplates(reader=template_views, capabilities=capabilities),
            ),
            build_devices_router(
                verify_token=verifier.verify,
                register=RegisterDevice(
                    unit_of_work=lambda: SqlAlchemyDeviceUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
                revoke=RevokeDevice(
                    unit_of_work=lambda: SqlAlchemyDeviceUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
                list_devices=ListMyDevices(
                    reader=SqlAlchemyDeviceReader(writes), capabilities=capabilities
                ),
            ),
            build_measurements_router(
                verify_token=verifier.verify,
                ingest=IngestMeasurements(
                    unit_of_work=lambda: SqlAlchemyMeasurementUnitOfWork(writes),
                    capabilities=capabilities,
                    thresholds=thresholds,
                    clock=clock,
                ),
            ),
            build_monitoring_settings_router(
                verify_token=verifier.verify,
                get_settings=GetMonitoringSettings(resolve=thresholds, capabilities=capabilities),
                update_settings=UpdateMonitoringSettings(
                    unit_of_work=lambda: SqlAlchemyMonitoringSettingsUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
            ),
            build_session_reminders_router(
                verify_token=verifier.verify,
                get_rules=GetSessionReminderRules(capabilities=capabilities),
                get_preferences=GetSessionReminderPreferences(
                    unit_of_work=lambda: SqlAlchemySessionReminderPreferencesUnitOfWork(writes),
                    capabilities=capabilities,
                ),
                update_preferences=UpdateSessionReminderPreferences(
                    unit_of_work=lambda: SqlAlchemySessionReminderPreferencesUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
            ),
        ],
        shutdown_hooks=[
            engine.dispose,
            read_engine.dispose,
            users_http.close,
            affiliation_http.close,
        ],
    )
