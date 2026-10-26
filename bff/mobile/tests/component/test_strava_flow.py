"""Componente HU031 en móvil (ADR-042): BFF → Usuarios (sesión y JWKS) → Afiliación (capacidad
`integrations.strava.connect` del plan `FREE`) → Gestión de terceros (base `terceros` y
simulador de Strava, nunca Strava real) → callback público del BFF → sincronización →
`terceros-sync-worker` → outbox → Pub/Sub → `entrenamientos-external-activity-consumer` →
historial de HU011 y tablero de HU033 con `source = STRAVA`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El tramo de mensajería
usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio. Ningún token de
Strava, `code` ni `client_secret` puede aparecer en las respuestas del BFF ni en los logs.
"""

import base64
import contextlib
import json
import logging
import os
import secrets
import time
import unittest
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock as AffiliationClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from third_party_management.infrastructure.messaging.pubsub import (
    PubSubEventPublisher as ThirdPartyPublisher,
)
from third_party_management.infrastructure.persistence.models import (
    ExternalAccountRow,
    OutboxEventRow,
)
from third_party_management.infrastructure.persistence.outbox import (
    SqlAlchemyOutboxStore as ThirdPartyOutboxStore,
)
from third_party_management.main import create_app as create_third_party
from third_party_management.settings import (
    TERCEROS_TOPIC,
    SyncWorkerSettings,
    ThirdPartyManagementSettings,
)
from third_party_management.settings import (
    OutboxPublisherSettings as ThirdPartyOutboxSettings,
)
from third_party_management.testing.strava_simulator import StravaSimulator, activity
from third_party_management.workers.outbox_publisher import OutboxRelay as ThirdPartyRelay
from third_party_management.workers.sync_worker import build_run_sync
from training_management.application.projections.external_activity_projection import (
    ProjectExternalActivity,
)
from training_management.infrastructure.clock import SystemClock as TrainingClock
from training_management.infrastructure.persistence_read.external_activities import (
    SqlAlchemyExternalActivityUnitOfWork,
)
from training_management.infrastructure.persistence_read.models import TrainingSessionHistoryRow
from training_management.main import create_app as create_training
from training_management.settings import EXTERNAL_ACTIVITY_SUBSCRIPTION, TrainingManagementSettings
from training_management.workers.external_activity_consumer import (
    ExternalActivityMessageHandler,
    ensure_resources,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

HISTORIES = {
    "users_w": "users_management.infrastructure.persistence_write:migrations",
    "users_r": "users_management.infrastructure.persistence_read:migrations",
    "affiliation": "affiliation_management.infrastructure.persistence:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
    "third_party": "third_party_management.infrastructure.persistence:migrations",
}
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
CLIENT_ID = "4242"
CONSENT_VERSION = "strava-consent-test.v1"
REDIRECT_URI = "https://bff-mobile/v1/integrations/strava/callback"
SCOPES = "read,activity:read"
STRAVA = "/v1/me/integrations/strava"
SYNCS = "/v1/me/integrations/strava/syncs"
CALLBACK = "/v1/integrations/strava/callback"
DEEP_LINK = "sportapp://integrations/strava?result="


def utc_seconds(delta: timedelta) -> str:
    instant = datetime.now(UTC).replace(microsecond=0) + delta
    return instant.isoformat().replace("+00:00", "Z")


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None)
        self.lines.append(f"{record.getMessage()} {json.dumps(fields, default=str)}")


@dataclass
class _Redelivery:
    """Mensaje de Pub/Sub entregado otra vez con los mismos bytes (al menos una vez)."""

    data: bytes
    message_id: str = "redelivery"
    delivery_attempt: int | None = 2
    acked: bool = False
    nacked: bool = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class StravaFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu031_{name}")) for name in HISTORIES
        }
        for name, history in HISTORIES.items():
            upgrade(history, self.urls[name])
        self.strava = StravaSimulator()
        self.client_secret = secrets.token_hex(16)
        self.token_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        self.users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["users_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["users_r"],  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
        self.training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        health_data_consent_version="local-dev",
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                ),
                base_url="http://training-management",
            )
        )
        third_party_values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.urls["third_party"],
            "users_management_base_url": "http://users-management",
            "affiliation_management_base_url": "http://affiliation-management",
            "strava_client_id": CLIENT_ID,
            "strava_client_secret": self.client_secret,
            "strava_token_key": self.token_key,
            "strava_redirect_uri": REDIRECT_URI,
            "strava_consent_version": CONSENT_VERSION,
        }
        third_party = self.enterContext(
            TestClient(
                create_third_party(
                    ThirdPartyManagementSettings(**third_party_values),
                    users_http=self.users,
                    affiliation_http=affiliation,
                    strava_http=self.strava.client(),
                ),
                base_url="http://third-party-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffMobileSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        third_party_management_base_url="http://third-party-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                    training_http=self.training,
                    third_party_http=third_party,
                ),
                base_url="https://bff-mobile",
            )
        )
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)
        self.responses: list[httpx.Response] = []
        # La captura se instala después de crear las apps: `create_service_app` reconfigura el
        # log raíz (`configure_logging` reemplaza sus manejadores) y la descartaría.
        self.capture = _Capture()
        root, quiet = logging.getLogger(), logging.getLogger("sqlalchemy")
        levels = (root.level, quiet.level)
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        quiet.setLevel(logging.WARNING)
        self.addCleanup(quiet.setLevel, levels[1])
        self.addCleanup(root.setLevel, levels[0])
        self.addCleanup(root.removeHandler, self.capture)

    # ----------------------------------------------------------------- ayudantes

    def register(self, actor_type: str, *, free_plan: bool = True) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user_id"]
        if actor_type == "athlete" and free_plan:
            # Lo que haría el consumidor del alta gratuita (ADR-024): plan `FREE` activo.
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def call(
        self, method: str, path: str, user: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if user is not None:
            headers["Authorization"] = user["Authorization"]
        response = self.bff.request(method, path, headers=headers, **kwargs)
        self.responses.append(response)
        return response

    def authorize(self, user: dict[str, str]) -> str:
        """Inicia la vinculación y devuelve el `state` de la URL de consentimiento."""
        started = self.call("POST", f"{STRAVA}/authorization", user)
        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual(started.headers["Cache-Control"], "no-store")
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        self.assertEqual(query["scope"], [SCOPES])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        return str(query["state"][0])

    def callback(self, **params: str) -> str:
        """Callback público que sigue Strava; devuelve el `result` del enlace profundo."""
        response = self.call("GET", CALLBACK, params=params, follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        location = str(response.headers["Location"])
        self.assertTrue(location.startswith(DEEP_LINK), location)
        return location[len(DEEP_LINK) :]

    def link(self, user: dict[str, str]) -> None:
        state = self.authorize(user)
        code = secrets.token_urlsafe(16)
        self.assertEqual(self.callback(code=code, state=state, scope=SCOPES), "LINKED")

    def request_sync(self, user: dict[str, str], key: str | None = None) -> Any:
        return self.call("POST", SYNCS, user, headers={"Idempotency-Key": key or str(uuid.uuid4())})

    def run_worker(self) -> Any:
        """Un ciclo de `terceros-sync-worker` con su cableado real sobre el simulador."""
        values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.urls["third_party"],
            "strava_client_id": CLIENT_ID,
            "strava_client_secret": self.client_secret,
            "strava_token_key": self.token_key,
            "worker_id": "terceros-sync-component",
        }
        outcome = build_run_sync(
            SyncWorkerSettings(**values),
            sessionmaker(self.engines["third_party"], expire_on_commit=False),
            self.strava.client(),
        )()
        assert outcome is not None
        return outcome

    def count(self, name: str, model: Any) -> int:
        with sessionmaker(self.engines[name])() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def strava_history_rows(self) -> int:
        with sessionmaker(self.engines["training_r"])() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(TrainingSessionHistoryRow)
                    .where(TrainingSessionHistoryRow.source == "STRAVA")
                )
                or 0
            )

    def assert_no_strava_secrets(self) -> None:
        sensitive = [*self.strava.sensitive_values(), self.client_secret, self.token_key]
        self.assertTrue(self.strava.issued_tokens, "el simulador debió emitir tokens")
        logs = "\n".join(self.capture.lines)
        for response in self.responses:
            exposed = f"{response.headers} {response.text}"
            for value in sensitive:
                self.assertNotIn(value, exposed, f"{response.request.url.path} expone un secreto")
        for value in sensitive:
            self.assertNotIn(value, logs)

    # ------------------------------------------------------------------- pruebas

    def test_link_sync_and_unlink_through_the_mobile_channel(self) -> None:
        """CA1-CA4, CA9: consentimiento oficial, callback con `LINKED`, `state` de un solo
        uso, sincronización idempotente (`202`), corrida del worker y desvinculación."""
        athlete = self.register("athlete")
        before = self.call("GET", STRAVA, athlete)
        state = self.authorize(athlete)
        code = secrets.token_urlsafe(16)

        linked = self.callback(code=code, state=state, scope=SCOPES)
        reused = self.callback(code=code, state=state, scope=SCOPES)

        self.assertEqual((before.status_code, before.json()["status"]), (200, "NOT_LINKED"))
        self.assertEqual((linked, reused), ("LINKED", "INVALID_STATE"))
        self.assertEqual(len(self.strava.calls("token")), 1, "el `code` se canjea una vez")
        status = self.call("GET", STRAVA, athlete)
        self.assertEqual(status.headers["Cache-Control"], "no-store")
        self.assertEqual(
            status.json(),
            {
                "status": "ACTIVE",
                "external_athlete_id": str(self.strava.athlete_id),
                "linked_at": status.json()["linked_at"],
                "scopes": ["read", "activity:read"],
                "last_sync": None,
            },
        )

        self.strava.activities = [
            activity(9001, start_date=utc_seconds(-timedelta(hours=3))),
            activity(9002, sport_type="Ride", start_date=utc_seconds(-timedelta(hours=2))),
        ]
        key = str(uuid.uuid4())
        accepted = self.request_sync(athlete, key)
        replayed = self.request_sync(athlete, key)
        busy = self.request_sync(athlete)

        self.assertEqual((accepted.status_code, accepted.json()["status"]), (202, "QUEUED"))
        self.assertEqual(replayed.json()["sync_run_id"], accepted.json()["sync_run_id"])
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual((busy.status_code, busy.json()["code"]), (409, "sync_in_progress"))

        outcome = self.run_worker()

        self.assertEqual(str(outcome.sync_run_id), accepted.json()["sync_run_id"])
        self.assertEqual((outcome.status, outcome.imported_count), ("SUCCEEDED", 2))
        self.assertEqual(self.count("third_party", OutboxEventRow), 2)
        last_sync = self.call("GET", STRAVA, athlete).json()["last_sync"]
        self.assertEqual(
            (last_sync["sync_run_id"], last_sync["status"], last_sync["imported_count"]),
            (accepted.json()["sync_run_id"], "SUCCEEDED", 2),
        )
        self.assertIsNone(last_sync["error_code"])

        unlinked = self.call("DELETE", STRAVA, athlete)
        again = self.call("DELETE", STRAVA, athlete)
        after = self.call("GET", STRAVA, athlete)
        not_linked = self.request_sync(athlete)

        self.assertEqual((unlinked.status_code, again.status_code), (204, 204))
        self.assertEqual(after.json()["status"], "REVOKED")
        self.assertEqual(
            (not_linked.status_code, not_linked.json()["code"]), (409, "integration_not_linked")
        )
        self.assertEqual(len(self.strava.calls("deauthorize")), 1)
        with sessionmaker(self.engines["third_party"])() as session:
            account = session.get(ExternalAccountRow, uuid.UUID(athlete["user_id"]))
        assert account is not None
        self.assertEqual((account.access_token_enc, account.refresh_token_enc), (None, None))
        self.assertEqual(self.count("third_party", OutboxEventRow), 2, "se conservan")
        self.assert_no_strava_secrets()

    def test_denied_and_forged_callbacks_redirect_with_the_result(self) -> None:
        """CA3: rechazo del usuario → `DENIED`; sin `state` o con uno inventado →
        `INVALID_STATE`; en todos los casos `302` al enlace profundo, nunca un error JSON."""
        athlete = self.register("athlete")

        denied = self.callback(state=self.authorize(athlete), error="access_denied")
        no_scope = self.callback(code="x", state=self.authorize(athlete), scope="read")
        missing = self.callback(code="x")
        forged = self.callback(code="x", state="no-es-un-state-emitido")

        self.assertEqual(
            (denied, no_scope, missing, forged),
            ("DENIED", "DENIED", "INVALID_STATE", "INVALID_STATE"),
        )
        self.assertEqual(self.strava.calls("token"), [])
        self.assertEqual(self.call("GET", STRAVA, athlete).json()["status"], "NOT_LINKED")

    def test_actor_and_capability_are_enforced(self) -> None:
        provider = self.register("service_provider")
        no_plan = self.register("athlete", free_plan=False)

        edge = self.call("POST", f"{STRAVA}/authorization", provider)
        capability = self.call("POST", f"{STRAVA}/authorization", no_plan)
        sync = self.request_sync(no_plan)

        self.assertEqual((edge.status_code, edge.json()["code"]), (403, "permission_required"))
        self.assertEqual(
            (capability.status_code, capability.json()["code"]), (403, "capability_required")
        )
        self.assertEqual((sync.status_code, sync.json()["code"]), (403, "capability_required"))

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_imported_activities_reach_history_and_dashboard_as_strava(self) -> None:
        """CA4, CA5 y CA8: el evento llega a Entrenamientos por Pub/Sub; el historial (HU011) y
        el tablero (HU033) muestran `source = STRAVA`; la reentrega no duplica."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete = self.register("athlete")
        self.link(athlete)
        self.strava.activities = [
            activity(9101, start_date=utc_seconds(-timedelta(hours=3))),
            activity(
                9102,
                sport_type="Ride",
                distance=25000.5,
                start_date=utc_seconds(-timedelta(hours=2)),
            ),
        ]
        self.assertEqual(self.request_sync(athlete).status_code, 202)
        self.assertEqual(self.run_worker().imported_count, 2)

        self.publish(project_id)
        delivered: list[bytes] = []
        self.assertTrue(
            self.consume(project_id, delivered, lambda: self.strava_history_rows() == 2),
            "el consumidor no proyectó las actividades importadas",
        )

        history = self.training.get(
            "/v1/training-history", headers={"Authorization": athlete["Authorization"]}
        )
        self.assertEqual(history.status_code, 200, history.text)
        items = {item["session_id"]: item for item in history.json()["items"]}
        running = items[str(uuid5(NAMESPACE_URL, "strava:9101"))]
        riding = items[str(uuid5(NAMESPACE_URL, "strava:9102"))]
        self.assertEqual((running["source"], running["sport"]), ("STRAVA", "running"))
        self.assertEqual((riding["source"], riding["sport"]), ("STRAVA", "cycling"))
        self.assertEqual(running["duration_seconds"], 3600)
        dashboard = self.call("GET", "/v1/me/training-dashboard", athlete)
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(dashboard.json()["by_source"]["STRAVA"]["sessions_count"], 2)
        self.assertEqual(dashboard.json()["by_source"]["SPORTAPP"]["sessions_count"], 0)

        # Al menos una vez: la reentrega del mismo mensaje se confirma sin efectos.
        self.assertTrue(delivered)
        redelivery = _Redelivery(delivered[0])
        ExternalActivityMessageHandler(self.projector())(redelivery)
        self.assertEqual((redelivery.acked, redelivery.nacked), (True, False))
        self.assertEqual(self.strava_history_rows(), 2)

        # CA5: la misma actividad en otra corrida no publica nada nuevo.
        self.assertEqual(self.request_sync(athlete).status_code, 202)
        self.assertEqual(self.run_worker().imported_count, 0)
        self.assertEqual(self.count("third_party", OutboxEventRow), 2)
        self.assert_no_strava_secrets()

    def publish(self, project_id: str) -> None:
        publisher = ThirdPartyPublisher(project_id, TERCEROS_TOPIC)
        values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.urls["third_party"],
            "pubsub_project_id": project_id,
            "worker_id": "terceros-outbox-component",
        }
        try:
            published = ThirdPartyRelay(
                ThirdPartyOutboxStore(sessionmaker(self.engines["third_party"])),
                publisher,
                ThirdPartyOutboxSettings(**values),
            ).run_once()
        finally:
            publisher.close()
        self.assertEqual(published, self.count("third_party", OutboxEventRow))

    def projector(self) -> ProjectExternalActivity:
        return ProjectExternalActivity(
            unit_of_work=lambda: SqlAlchemyExternalActivityUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=TrainingClock(),
        )

    def consume(self, project_id: str, delivered: list[bytes], until: Callable[[], bool]) -> bool:
        subscriber = pubsub_v1.SubscriberClient()
        handler = ExternalActivityMessageHandler(self.projector())

        def callback(message: Any) -> None:
            delivered.append(bytes(message.data))
            handler(message)

        streaming = subscriber.subscribe(
            subscriber.subscription_path(project_id, EXTERNAL_ACTIVITY_SUBSCRIPTION),
            callback=callback,
        )
        try:
            return wait_until(until)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)
            subscriber.close()


if __name__ == "__main__":
    unittest.main()
