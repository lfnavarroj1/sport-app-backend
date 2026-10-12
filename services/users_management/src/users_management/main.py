"""Aplicación FastAPI de `users_management`."""

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from users_management.api.lookup import build_lookup_router
from users_management.api.registration import build_registration_router
from users_management.api.sessions import build_jwks_router, build_sessions_router
from users_management.application.commands.login import Login
from users_management.application.commands.refresh_session import RefreshSession
from users_management.application.commands.register_user import RegisterUser
from users_management.application.commands.revoke_session import RevokeSession
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.domain.authentication.model import LoginThrottlePolicy
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_read.directory import SqlAlchemyUserDirectory
from users_management.infrastructure.persistence_write.authentication import (
    SqlAlchemyAuthenticationUnitOfWork,
)
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer
from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher
from users_management.settings import UsersManagementSettings

SERVICE_NAME = "users-management"
API_VERSION = "0.5.0"


def create_app(settings: UsersManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(UsersManagementSettings)
    engine = create_database_engine(settings.database_write_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    # Consultas (HU010) desde la base de lectura; el motor es perezoso y no afecta readiness.
    read_engine = create_database_engine(settings.database_read_url, settings)
    lookup_user = LookupUserByEmail(SqlAlchemyUserDirectory(sessionmaker(read_engine)))
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
            build_lookup_router(lookup_user),
        ],
        shutdown_hooks=[engine.dispose, read_engine.dispose],
    )
