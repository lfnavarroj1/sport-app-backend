"""Base `terceros` migrada, configuración sintética de Strava y utilidades de las pruebas de
integración de HU031 (ADR-042) contra PostgreSQL real (`TEST_DATABASE_URL`)."""

import base64
import logging
import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from third_party_management.application.ports import OutboxMessage, ProviderTokens
from third_party_management.domain.connections import ExternalAccount, SealedTokens
from third_party_management.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from third_party_management.infrastructure.token_cipher import AesGcmTokenSealer
from third_party_management.settings import ThirdPartyManagementSettings

HISTORY = "third_party_management.infrastructure.persistence:migrations"
HEAD = "0001_strava_connections"
CONSENT_VERSION = "strava-consent-2026-10.v1"
REDIRECT_URI = "https://bff-mobile.sportapp.test/v1/integrations/strava/callback"
CLIENT_ID = "4242"
USERS_BASE_URL = "http://users-management"
AFFILIATION_BASE_URL = "http://affiliation-management"


def synthetic_token_key() -> str:
    """`STRAVA_TOKEN_KEY` sintética: 32 bytes aleatorios en base64 URL-safe."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def settings_for(url: str, *, configured: bool = True) -> ThirdPartyManagementSettings:
    """Configuración del servicio. Sin `configured` las `STRAVA_*` quedan ausentes de forma
    explícita (no se heredan del entorno)."""
    strava: dict[str, Any] = {
        "strava_client_id": CLIENT_ID if configured else None,
        "strava_client_secret": secrets.token_hex(16) if configured else None,
        "strava_token_key": synthetic_token_key() if configured else None,
        "strava_redirect_uri": REDIRECT_URI if configured else None,
        "strava_consent_version": CONSENT_VERSION if configured else None,
    }
    return ThirdPartyManagementSettings(
        app_env="test",  # type: ignore[arg-type]
        database_url=url,  # type: ignore[arg-type]
        users_management_base_url=USERS_BASE_URL,  # type: ignore[arg-type]
        affiliation_management_base_url=AFFILIATION_BASE_URL,  # type: ignore[arg-type]
        **strava,
    )


@dataclass
class ThirdPartyDatabase:
    url: str
    engine: Engine
    sessions: sessionmaker[Session]

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.sessions)

    def count(self, model: Any, *conditions: Any) -> int:
        with self.sessions() as session:
            statement = select(func.count()).select_from(model)
            if conditions:
                statement = statement.where(*conditions)
            return session.scalar(statement) or 0

    def row(self, model: Any, *conditions: Any) -> Any:
        """Una fila del ORM (o `None`), leída en una sesión que se cierra al terminar."""
        with self.sessions() as session:
            return session.execute(select(model).where(*conditions)).scalar_one_or_none()


@contextmanager
def third_party_database(prefix: str) -> Iterator[ThirdPartyDatabase]:
    with temporary_database(prefix) as url:
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=10, max_overflow=10)
        try:
            yield ThirdPartyDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
        finally:
            engine.dispose()


def plain_tokens(lifetime: timedelta = timedelta(hours=6)) -> ProviderTokens:
    """Tokens sintéticos en claro, generados al azar en cada llamada."""
    return ProviderTokens(
        access_token=secrets.token_urlsafe(24),
        refresh_token=secrets.token_urlsafe(24),
        expires_at=datetime.now(UTC).replace(microsecond=0) + lifetime,
    )


def sealer() -> AesGcmTokenSealer:
    return AesGcmTokenSealer(secrets.token_bytes(32))


def linked_account(
    user_id: UUID,
    external_athlete_id: str,
    sealed: SealedTokens,
    *,
    now: datetime | None = None,
    current: ExternalAccount | None = None,
) -> ExternalAccount:
    return ExternalAccount.link(
        current=current,
        user_id=user_id,
        external_athlete_id=external_athlete_id,
        tokens=sealed,
        scopes=("read", "activity:read"),
        consent_version=CONSENT_VERSION,
        now=now or datetime.now(UTC).replace(microsecond=0),
    )


def outbox_message(aggregate_id: UUID | None = None) -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        event_type="actividad.importada.v1",
        schema_version=1,
        aggregate_type="imported_activity",
        aggregate_id=aggregate_id or uuid4(),
        aggregate_version=1,
        occurred_at=datetime.now(UTC),
        correlation_id=uuid4(),
        causation_id=str(uuid4()),
        payload={"provider": "STRAVA"},
    )


def leaks(text: str, sensitive: Iterable[str]) -> list[str]:
    """Valores sensibles que aparecen en el texto (vacíos ignorados)."""
    return [value for value in sensitive if value and value in text]


class LogCapture(logging.Handler):
    """Captura del log raíz. Se instala después de crear la aplicación: `create_service_app`
    reemplaza los manejadores del log raíz y descartaría una captura previa."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(
            f"{record.name} {record.getMessage()} {getattr(record, 'fields', '')} {record.args}"
            for record in self.records
        )

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]
