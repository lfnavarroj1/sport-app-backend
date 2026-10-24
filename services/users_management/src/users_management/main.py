"""Aplicación FastAPI de `users_management`."""

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import AccessTokenVerifier
from users_management.api.lookup import build_lookup_router
from users_management.api.nutrition_restrictions import build_nutrition_restrictions_router
from users_management.api.profile import build_profile_router
from users_management.api.registration import build_registration_router
from users_management.api.sessions import build_jwks_router, build_sessions_router
from users_management.application.commands.login import Login
from users_management.application.commands.refresh_session import RefreshSession
from users_management.application.commands.register_user import RegisterUser
from users_management.application.commands.revoke_session import RevokeSession
from users_management.application.commands.update_demographic_profile import (
    UpdateDemographicProfile,
)
from users_management.application.commands.update_nutrition_habits import UpdateNutritionHabits
from users_management.application.commands.update_physiological_profile import (
    UpdatePhysiologicalProfile,
)
from users_management.application.commands.update_sport_habits import UpdateSportHabits
from users_management.application.queries.get_profile import GetAthleteProfile
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.application.queries.lookup_users_batch import LookupUsersByIds
from users_management.application.queries.nutrition_restrictions import (
    GetNutritionRestrictions,
)
from users_management.domain.authentication.model import LoginThrottlePolicy
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_read.directory import SqlAlchemyUserDirectory
from users_management.infrastructure.persistence_read.nutrition_restrictions import (
    SqlAlchemyNutritionRestrictionsReader,
)
from users_management.infrastructure.persistence_read.profile import SqlAlchemyAthleteProfileReader
from users_management.infrastructure.persistence_write.authentication import (
    SqlAlchemyAuthenticationUnitOfWork,
)
from users_management.infrastructure.persistence_write.profile import SqlAlchemyProfileUnitOfWork
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer
from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher
from users_management.settings import UsersManagementSettings

SERVICE_NAME = "users-management"
API_VERSION = "0.10.0"


def create_app(settings: UsersManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(UsersManagementSettings)
    engine = create_database_engine(settings.database_write_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    # Consultas (HU010) desde la base de lectura; el motor es perezoso y no afecta readiness.
    read_engine = create_database_engine(settings.database_read_url, settings)
    read_sessions = sessionmaker(read_engine)
    directory = SqlAlchemyUserDirectory(read_sessions)
    lookup_user = LookupUserByEmail(directory)
    lookup_batch = LookupUsersByIds(directory)
    clock = SystemClock()
    passwords = Argon2idPasswordHasher()
    register_user = RegisterUser(
        unit_of_work=lambda: SqlAlchemyRegistrationUnitOfWork(sessions),
        password_hasher=passwords,
        clock=clock,
        current_policies_version=settings.registration_policies_version,
    )
    token_issuer = Ed25519AccessTokenIssuer(
        settings.signing_key(),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        ttl=settings.access_token_ttl,
    )
    login = Login(
        unit_of_work=lambda: SqlAlchemyAuthenticationUnitOfWork(sessions),
        password_verifier=passwords,
        token_issuer=token_issuer,
        clock=clock,
        throttle=LoginThrottlePolicy(
            max_failures=settings.login_max_failures, window=settings.login_failure_window
        ),
        refresh_token_ttl=settings.refresh_token_ttl,
    )
    refresh_session = RefreshSession(
        unit_of_work=lambda: SqlAlchemyAuthenticationUnitOfWork(sessions),
        token_issuer=token_issuer,
        clock=clock,
        refresh_token_ttl=settings.refresh_token_ttl,
    )
    # HU005: el servicio verifica sus propios tokens con su JWKS local (sin red).
    verifier = AccessTokenVerifier(
        token_issuer.jwks(), issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    profile_router = build_profile_router(
        verify_token=verifier.verify,
        update_demographic=UpdateDemographicProfile(
            unit_of_work=lambda: SqlAlchemyProfileUnitOfWork(sessions), clock=clock
        ),
        update_physiological=UpdatePhysiologicalProfile(
            unit_of_work=lambda: SqlAlchemyProfileUnitOfWork(sessions),
            clock=clock,
            current_consent_version=settings.health_data_consent_version,
        ),
        update_sport_habits=UpdateSportHabits(
            unit_of_work=lambda: SqlAlchemyProfileUnitOfWork(sessions), clock=clock
        ),
        update_nutrition_habits=UpdateNutritionHabits(
            unit_of_work=lambda: SqlAlchemyProfileUnitOfWork(sessions),
            clock=clock,
            current_consent_version=settings.health_data_consent_version,
        ),
        get_profile=GetAthleteProfile(SqlAlchemyAthleteProfileReader(read_sessions)),
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Usuarios",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada.
        readiness_checks=[DatabaseReadinessCheck("database_write", engine)],
        routers=[
            build_registration_router(register_user),
            build_sessions_router(
                login,
                refresh_session,
                RevokeSession(
                    unit_of_work=lambda: SqlAlchemyAuthenticationUnitOfWork(sessions), clock=clock
                ),
            ),
            build_jwks_router(token_issuer.jwks()),
            build_lookup_router(lookup_user, lookup_batch),
            build_nutrition_restrictions_router(
                GetNutritionRestrictions(SqlAlchemyNutritionRestrictionsReader(read_sessions))
            ),
            profile_router,
        ],
        shutdown_hooks=[engine.dispose, read_engine.dispose],
    )
