"""Aplicación FastAPI de `users_management`."""

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from users_management.api.registration import build_registration_router
from users_management.application.commands.register_user import RegisterUser
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyRegistrationUnitOfWork,
)
from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher
from users_management.settings import UsersManagementSettings

SERVICE_NAME = "users-management"
API_VERSION = "0.2.0"


def create_app(settings: UsersManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(UsersManagementSettings)
    engine = create_database_engine(settings.database_write_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    register_user = RegisterUser(
        unit_of_work=lambda: SqlAlchemyRegistrationUnitOfWork(sessions),
        password_hasher=Argon2idPasswordHasher(),
        clock=SystemClock(),
        current_policies_version=settings.registration_policies_version,
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Usuarios",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada.
        readiness_checks=[DatabaseReadinessCheck("database_write", engine)],
        routers=[build_registration_router(register_user)],
        shutdown_hooks=[engine.dispose],
    )
