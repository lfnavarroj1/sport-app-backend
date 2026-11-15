"""Componente HU040 (ADR-046): `bff-mobile` → Usuarios (sesión y JWKS) → Afiliación
(capacidad `services.virtual_sessions.schedule`) → `complementary-services` → PostgreSQL
migrado → outbox → `servicios-outbox-publisher` → emulador de Pub/Sub; y `bff-web` →
`complementary-services` + directorio de Usuarios para el profesional (lista y calendario
integrado). Requiere `TEST_DATABASE_URL`; el tramo de mensajería, `PUBSUB_EMULATOR_HOST`.

Todo es real: el registro y el inicio de sesión de HU001/HU003, la creación y publicación del
servicio de HU015/HU019, el cambio a `PREMIUM` de HU016 y el directorio proyectado con
`usuarios-read-projector` sin Pub/Sub.

Trazabilidad: CA1 y CA5 (`test_premium_athlete_books_retries_and_cancels`); CA2
(`test_capacity_one_and_duplicate`); CA3 (`test_without_capability_and_ineligible_service`);
CA4 (`test_premium_athlete_books_retries_and_cancels`, `test_cancellation_two_hours_before`);
CA6 (`test_provider_sees_the_appointment_with_the_athlete_name`); recorrido BFF → servicio →
evento (`test_events_reach_the_emulator`).
"""

import json
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, select
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
from bff_mobile.main import create_app as create_mobile
from bff_mobile.settings import BffMobileSettings
from bff_web.main import create_app as create_web
from bff_web.settings import BffWebSettings
from complementary_services.infrastructure.messaging.pubsub import PubSubEventPublisher
from complementary_services.infrastructure.persistence.models import OutboxEventRow
from complementary_services.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from complementary_services.main import create_app as create_services
from complementary_services.settings import (
    SERVICES_TOPIC,
    ComplementaryServicesSettings,
    OutboxPublisherSettings,
)
from complementary_services.workers.outbox_publisher import OutboxRelay
from complementary_services.workers.pubsub_setup import ensure_resources
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.contracts import repository_root
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock as UsersClock
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork as UsersProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as UsersOutboxStore,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings
from users_management.workers.outbox_publisher import build_envelope as users_envelope

USERS_WRITE = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
SERVICES = "complementary_services.infrastructure.persistence:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
BOGOTA = ZoneInfo("America/Bogota")
APPOINTMENTS = "/v1/me/appointments"
SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "servicios"


def virtual_offer(
    *starts: datetime, capacity: int = 1, modality: str = "VIRTUAL"
) -> dict[str, Any]:
    return {
        "name": "Entrenamiento funcional virtual",
        "description": "Sesión virtual sintética con un entrenador para pruebas de componente.",
        "category": "COACHING",
        "modality": modality,
        "price": {"amount": "80000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": start.astimezone(BOGOTA).isoformat(),
                "ends_at": (start + timedelta(hours=1)).astimezone(BOGOTA).isoformat(),
                "time_zone": "America/Bogota",
                "capacity": capacity,
            }
            for start in starts
        ],
    }


def soon(hours: float) -> datetime:
    """Instante futuro redondeado al minuto (los intervalos se declaran con desfase)."""
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(second=0, microsecond=0)


class AppointmentsFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        urls = {
            name: self.enterContext(temporary_database(f"hu040_flow_{name}"))
            for name in ("users_w", "users_r", "services", "affiliation")
        }
        for history, name in (
            (USERS_WRITE, "users_w"),
            (USERS_READ, "users_r"),
            (SERVICES, "services"),
            (AFFILIATION, "affiliation"),
        ):
            upgrade(history, urls[name])
        self.engines = {name: create_engine(url) for name, url in urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)
        self.services_url = urls["services"]
        users = self.client(
            create_users(
                UsersManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=urls["users_w"],  # type: ignore[arg-type]
                    database_read_url=urls["users_r"],  # type: ignore[arg-type]
                    registration_policies_version=POLICIES,
                    health_data_consent_version="health-test.v1",
                    jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                )
            ),
            "http://users-management",
        )
        self.users = users
        affiliation = self.client(
            create_affiliation(
                AffiliationManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=urls["affiliation"],  # type: ignore[arg-type]
                    affiliation_expected_catalog_version=CATALOG,
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                ),
                users_http=users,
            ),
            "http://affiliation-management",
        )
        self.affiliation = affiliation
        self.services = self.client(
            create_services(
                ComplementaryServicesSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=urls["services"],  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=users,
                affiliation_http=affiliation,
            ),
            "http://complementary-services",
        )
        unused = httpx.Client(base_url="http://unused", transport=httpx.MockTransport(_unused))
        # El calendario integrado del profesional también consulta Eventos; aquí no está.
        offline = httpx.Client(base_url="http://offline", transport=httpx.MockTransport(_offline))
        self.mobile = self.client(
            create_mobile(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                    third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
                ),
                users_http=users,
                affiliation_http=affiliation,
                training_http=unused,
                events_http=unused,
                services_http=self.services,
            ),
            "https://bff-mobile",
        )
        self.web = self.client(
            create_web(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                    third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    cors_allowed_origins=(ORIGIN,),
                ),
                users_http=users,
                affiliation_http=affiliation,
                services_http=self.services,
                events_http=offline,
                training_http=unused,
            ),
            "https://bff-web",
            headers={"Origin": ORIGIN},
        )

    def client(self, app: Any, base_url: str, **kwargs: Any) -> TestClient:
        return self.enterContext(TestClient(app, base_url=base_url, **kwargs))

    # ------------------------------------------------------------------ preparación

    def session_for(self, actor_type: str, full_name: str = "Persona Sintética") -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.web.post(
            "/v1/users",
            json={
                "full_name": full_name,
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        if actor_type == "athlete":
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(
                RegisteredUser(
                    uuid.uuid4(), uuid.UUID(registered.json()["user_id"]), "athlete", None
                )
            )
        login = self.web.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    def premium_athlete(self, full_name: str = "Ana Pérez") -> dict[str, str]:
        athlete = self.session_for("athlete", full_name)
        changed = self.web.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": "PREMIUM",
                "periodicity": "MONTHLY",
                "expected_subscription_version": 1,
            },
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        return athlete

    def publish(self, provider: dict[str, str], body: dict[str, Any]) -> str:
        created = self.web.post(
            "/v1/services", json=body, headers={**provider, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(created.status_code, 201, created.text)
        service_id: str = created.json()["service_id"]
        published = self.web.post(
            f"/v1/services/{service_id}/publish",
            headers={**provider, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(published.status_code, 200, published.text)
        return service_id

    def intervals(self, athlete: dict[str, str], service_id: str) -> list[dict[str, Any]]:
        """`GET /v1/services/availability` por `bff-mobile`: los intervalos del servicio."""
        start = datetime.now(UTC)
        response = self.mobile.get(
            "/v1/services/availability",
            params={
                "from": start.isoformat(),
                "to": (start + timedelta(days=30)).isoformat(),
                "category": "COACHING",
            },
            headers=athlete,
        )
        self.assertEqual(response.status_code, 200, response.text)
        [service] = [s for s in response.json()["items"] if s["service_id"] == service_id]
        intervals: list[dict[str, Any]] = service["availability"]
        return intervals

    def book(
        self,
        athlete: dict[str, str],
        service_id: str,
        availability_id: str,
        key: str | None = None,
    ) -> httpx.Response:
        return self.mobile.post(
            APPOINTMENTS,
            json={"service_id": service_id, "availability_id": availability_id},
            headers={**athlete, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def project_users(self) -> None:
        """`usuarios-outbox-publisher` + `usuarios-read-projector` sin Pub/Sub (directorio)."""
        outbox = UsersOutboxStore(sessionmaker(self.engines["users_w"]))
        project = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(sessionmaker(self.engines["users_r"])),
            clock=UsersClock(),
        )
        for event in outbox.claim_batch("w-test", 100, 30):
            project(parse_users_event(json.loads(json.dumps(users_envelope(event)))))
            outbox.mark_published(event.event_id, "w-test")

    def appointment_events(self) -> list[OutboxEventRow]:
        with sessionmaker(self.engines["services"])() as session:
            return list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.aggregate_type == "appointment")
                    .order_by(OutboxEventRow.occurred_at)
                )
            )

    # ------------------------------------------------------------------ pruebas

    def test_premium_athlete_books_retries_and_cancels(self) -> None:
        """CA1, CA4 y CA5 por el BFF móvil con la Afiliación real (`PREMIUM`)."""
        provider = self.session_for("service_provider")
        service_id = self.publish(provider, virtual_offer(soon(72), capacity=2))
        athlete = self.premium_athlete()
        [interval] = self.intervals(athlete, service_id)
        key = str(uuid.uuid4())

        created = self.book(athlete, service_id, interval["availability_id"], key)
        replayed = self.book(athlete, service_id, interval["availability_id"], key)
        listed = self.mobile.get(APPOINTMENTS, headers=athlete)
        appointment_id = created.json()["appointment_id"]
        cancelled = self.mobile.delete(f"{APPOINTMENTS}/{appointment_id}", headers=athlete)
        repeated = self.mobile.delete(f"{APPOINTMENTS}/{appointment_id}", headers=athlete)
        again = self.book(athlete, service_id, interval["availability_id"])

        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual((body["status"], body["meeting_url"]), ("BOOKED", None))
        self.assertEqual(body["time_zone"], "America/Bogota")
        self.assertEqual(body["starts_at"], interval["starts_at"])
        self.assertEqual((replayed.status_code, replayed.json()), (201, body))
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual([i["appointment_id"] for i in listed.json()["items"]], [appointment_id])
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "CANCELLED_BY_ATHLETE")
        self.assertEqual((repeated.status_code, repeated.json()), (200, cancelled.json()))
        self.assertEqual(again.status_code, 201, again.text)
        events = self.appointment_events()
        self.assertEqual(
            [event.event_type for event in events],
            ["sesion.programada.v1", "sesion.cancelada.v1", "sesion.programada.v1"],
        )

    def test_capacity_one_and_duplicate(self) -> None:
        """CA2: capacidad 1 → el segundo deportista recibe `availability_full`; el mismo
        deportista en otro intervalo con cupo y otra clave → `appointment_exists`."""
        provider = self.session_for("service_provider")
        single = self.publish(provider, virtual_offer(soon(48)))
        roomy = self.publish(provider, virtual_offer(soon(96), capacity=3))
        first, second = self.premium_athlete(), self.premium_athlete("Luis Gómez")
        [single_interval] = self.intervals(first, single)
        [roomy_interval] = self.intervals(first, roomy)

        booked = self.book(first, single, single_interval["availability_id"])
        full = self.book(second, single, single_interval["availability_id"])
        self.book(first, roomy, roomy_interval["availability_id"])
        duplicate = self.book(first, roomy, roomy_interval["availability_id"])

        self.assertEqual(booked.status_code, 201, booked.text)
        self.assertEqual((full.status_code, full.json()["code"]), (409, "availability_full"))
        self.assertEqual(
            (duplicate.status_code, duplicate.json()["code"]), (409, "appointment_exists")
        )

    def test_without_capability_and_ineligible_service(self) -> None:
        """CA3: `FREE` → `403 capability_required` en el borde; presencial →
        `422 service_not_bookable`; menos de 1 h → `422 availability_not_bookable`."""
        provider = self.session_for("service_provider")
        virtual = self.publish(provider, virtual_offer(soon(72)))
        in_person = self.publish(provider, virtual_offer(soon(72), modality="IN_PERSON"))
        imminent = self.publish(provider, virtual_offer(soon(0.5)))
        free = self.session_for("athlete")
        premium = self.premium_athlete()
        [virtual_interval] = self.intervals(premium, virtual)
        [in_person_interval] = self.intervals(premium, in_person)
        [imminent_interval] = self.intervals(premium, imminent)

        no_plan = self.book(free, virtual, virtual_interval["availability_id"])
        not_bookable = self.book(premium, in_person, in_person_interval["availability_id"])
        too_soon = self.book(premium, imminent, imminent_interval["availability_id"])

        self.assertEqual(
            (no_plan.status_code, no_plan.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (not_bookable.status_code, not_bookable.json()["code"]), (422, "service_not_bookable")
        )
        self.assertEqual(
            (too_soon.status_code, too_soon.json()["code"]), (422, "availability_not_bookable")
        )
        self.assertEqual(self.appointment_events(), [])

    def test_cancellation_two_hours_before(self) -> None:
        """CA4: una cita que empieza en 2 h no se cancela (`cancellation_window_closed`)."""
        provider = self.session_for("service_provider")
        service_id = self.publish(provider, virtual_offer(soon(2)))
        athlete = self.premium_athlete()
        [interval] = self.intervals(athlete, service_id)
        appointment_id = self.book(athlete, service_id, interval["availability_id"]).json()[
            "appointment_id"
        ]

        closed = self.mobile.delete(f"{APPOINTMENTS}/{appointment_id}", headers=athlete)

        self.assertEqual(
            (closed.status_code, closed.json()["code"]), (422, "cancellation_window_closed")
        )
        [listed] = self.mobile.get(APPOINTMENTS, headers=athlete).json()["items"]
        self.assertEqual(listed["status"], "BOOKED")

    def test_provider_sees_the_appointment_with_the_athlete_name(self) -> None:
        """CA6: el profesional ve la cita por `bff-web` (lista y fuente `OWN_APPOINTMENT` del
        calendario integrado) con el nombre del deportista y `meeting_url` nulo; otro
        profesional no la ve."""
        provider = self.session_for("service_provider")
        other = self.session_for("service_provider")
        start = soon(72)
        service_id = self.publish(provider, virtual_offer(start))
        athlete = self.premium_athlete("Ana Pérez")
        [interval] = self.intervals(athlete, service_id)
        booked = self.book(athlete, service_id, interval["availability_id"]).json()
        self.project_users()

        mine = self.web.get(APPOINTMENTS, headers=provider)
        foreign = self.web.get(APPOINTMENTS, headers=other)
        calendar = self.web.get(
            "/v1/me/integrated-calendar",
            params={
                "from": (start - timedelta(days=1)).isoformat(),
                "to": (start + timedelta(days=1)).isoformat(),
            },
            headers=provider,
        )

        self.assertEqual(mine.status_code, 200, mine.text)
        [item] = mine.json()["items"]
        self.assertEqual(
            (item["appointment_id"], item["athlete_display_name"], item["meeting_url"]),
            (booked["appointment_id"], "Ana Pérez", None),
        )
        self.assertEqual(mine.json()["sources"], [{"source": "users", "status": "OK"}])
        self.assertEqual(foreign.json()["items"], [])
        self.assertEqual(calendar.status_code, 200, calendar.text)
        own = [i for i in calendar.json()["items"] if i["source"] == "OWN_APPOINTMENT"]
        self.assertEqual(
            [(i["reference"]["appointment_id"], i["status"]) for i in own],
            [(booked["appointment_id"], "BOOKED")],
        )
        self.assertIn(
            {"name": "OWN_APPOINTMENT", "status": "OK", "truncated": False},
            calendar.json()["sources"],
        )

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_events_reach_the_emulator(self) -> None:
        """Recorrido BFF → servicio → outbox → `servicios-outbox-publisher` → emulador: los dos
        sobres cumplen su esquema, con `ordering_key = appointment_id`."""
        provider = self.session_for("service_provider")
        service_id = self.publish(provider, virtual_offer(soon(72)))
        athlete = self.premium_athlete()
        [interval] = self.intervals(athlete, service_id)
        appointment_id = self.book(athlete, service_id, interval["availability_id"]).json()[
            "appointment_id"
        ]
        self.mobile.delete(f"{APPOINTMENTS}/{appointment_id}", headers=athlete)
        project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        [topic] = ensure_resources(project)
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        listener = subscriber.subscription_path(project, f"test-{uuid.uuid4().hex[:6]}")
        subscriber.create_subscription(
            request={"name": listener, "topic": topic, "enable_message_ordering": True}
        )

        publisher = PubSubEventPublisher(project, SERVICES_TOPIC)
        try:
            published = OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.engines["services"])),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=self.services_url,  # type: ignore[arg-type]
                    pubsub_project_id=project,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()

        received: list[Any] = []
        for _ in range(5):
            received += subscriber.pull(
                request={"subscription": listener, "max_messages": 10}, timeout=30
            ).received_messages
            if len([m for m in received if m.message.ordering_key == appointment_id]) >= 2:
                break
        mine = [m.message for m in received if m.message.ordering_key == appointment_id]
        self.assertGreaterEqual(published, 3, "servicio.publicado.v1 + las dos de la cita")
        self.assertEqual(
            sorted(message.attributes["event_type"] for message in mine),
            ["sesion.cancelada.v1", "sesion.programada.v1"],
        )
        for message in mine:
            body = json.loads(message.data)
            schema = json.loads(
                (SCHEMAS / f"{body['event_type']}.schema.json").read_text(encoding="utf-8")
            )
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)
            self.assertEqual(body["aggregate_id"], appointment_id)


def _offline(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"code": "upstream_unavailable"})


def _unused(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"llamada inesperada: {request.url}")


if __name__ == "__main__":
    unittest.main()
