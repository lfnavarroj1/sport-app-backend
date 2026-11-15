"""Componente HU029 (ADR-041): `bff-mobile` → Afiliación (capacidad) → `complementary-services`
(`GET /v1/services/availability`) + `events-management` (`GET /v1/me/event-registrations`) +
Usuarios (`directory-lookup`), todos reales con PostgreSQL migrado (requiere
`TEST_DATABASE_URL`).

Mismo montaje que `test_event_registration_flow.py` (se importa el módulo, no la clase, para no
volver a descubrir sus pruebas). Los servicios se crean y publican con las operaciones reales
de HU015/HU019; la inscripción es la real de HU041 y se proyecta sin Pub/Sub. El evento
`PUBLISHED` se siembra como en HU041. El directorio de Usuarios se proyecta sin Pub/Sub.
"""

import base64
import json
import statistics
import time
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import test_event_registration_flow as registration_flow
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
from complementary_services.infrastructure.persistence.models import (
    ServiceAvailabilityRow,
    ServiceRow,
)
from complementary_services.main import create_app as create_services
from complementary_services.settings import ComplementaryServicesSettings
from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.infrastructure.persistence_write.models import EventRow
from sportapp_platform.migrations import upgrade
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
from users_management.workers.outbox_publisher import build_envelope as users_envelope

SUGGESTIONS = "/v1/me/suggestions/services"
SERVICES = "complementary_services.infrastructure.persistence:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
CATALOG = "plans-2026-10-13.v1"
BOGOTA = ZoneInfo("America/Bogota")
# Evento inscrito dentro de 10 días, de 07:00 a 11:00 en Bogotá.
EVENT_DAY = (datetime.now(BOGOTA) + timedelta(days=10)).date()


def local(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=BOGOTA)


def offer(name: str, category: str, *starts: datetime) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Servicio sintético para las sugerencias de acompañamiento.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "60000.00", "currency": "COP", "billing_unit": "SERVICE"},
        "availability": [
            {
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=2)).isoformat(),
                "time_zone": "America/Bogota",
                "capacity": 4,
            }
            for start in starts
        ],
    }


class ServiceSuggestionsFlowTest(unittest.TestCase):
    sign_up = registration_flow.EventRegistrationFlowTest.sign_up
    client = registration_flow.EventRegistrationFlowTest.client
    project_outbox = registration_flow.EventRegistrationFlowTest.project_outbox

    def setUp(self) -> None:
        registration_flow.EventRegistrationFlowTest.setUp(self)  # type: ignore[arg-type]
        services_url = self.enterContext(temporary_database("hu029_services"))
        affiliation_url = self.enterContext(temporary_database("hu029_affiliation"))
        upgrade(SERVICES, services_url)
        upgrade(AFFILIATION, affiliation_url)
        for name, url in (("services", services_url), ("affiliation", affiliation_url)):
            self.engines[name] = create_engine(url)
            self.addCleanup(self.engines[name].dispose)
        affiliation = self.client(
            create_affiliation(
                AffiliationManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=affiliation_url,  # type: ignore[arg-type]
                    affiliation_expected_catalog_version=CATALOG,
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
            ),
            "http://affiliation-management",
        )
        self.services = self.client(
            create_services(
                ComplementaryServicesSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=services_url,  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=affiliation,
            ),
            "http://complementary-services",
        )
        self.affiliation = affiliation
        self.mobile = self.mobile_with(events_http=self.events, services_http=self.services)

    def mobile_with(self, *, events_http: httpx.Client, services_http: httpx.Client) -> TestClient:
        return self.client(
            create_bff(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events-management",  # type: ignore[arg-type]
                    complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                    third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=self.affiliation,
                training_http=httpx.Client(
                    base_url="http://training",
                    transport=httpx.MockTransport(registration_flow.unused),
                ),
                events_http=events_http,
                services_http=services_http,
            ),
            "https://bff-mobile",
        )

    def athlete(self) -> dict[str, str]:
        """Deportista con su plan `FREE` (incluye `services.support.suggest`)."""
        user = self.sign_up("athlete")
        payload = user["Authorization"].split(".")[1]
        user_id = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]
        ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                sessionmaker(self.engines["affiliation"])
            ),
            clock=AffiliationClock(),
        )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), "athlete", None))
        return user

    def publish(
        self, provider: dict[str, str], body: dict[str, Any], *, publish: bool = True
    ) -> str:
        created = self.services.post(
            "/v1/services", json=body, headers={**provider, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(created.status_code, 201, created.text)
        service_id: str = created.json()["service_id"]
        if publish:
            published = self.services.post(
                f"/v1/services/{service_id}/publish",
                headers={**provider, "Idempotency-Key": str(uuid.uuid4())},
            )
            self.assertEqual(published.status_code, 200, published.text)
        return service_id

    def add_event(self, starts_at: datetime) -> str:
        """Evento `PUBLISHED` sintético en escritura y lectura (como en HU041)."""
        event_id, now = uuid.uuid4(), datetime.now(UTC)
        values: dict[str, Any] = {
            "event_id": event_id,
            "organizer_id": uuid.uuid4(),
            "name": "Media maratón de Bogotá",
            "description": "Carrera de veintiún kilómetros para aficionados.",
            "discipline": "Running",
            "location_name": "Parque Metropolitano Simón Bolívar",
            "location_address": "Avenida Calle 63 # 60-50",
            "location_city": "Bogotá",
            "location_country_code": "CO",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=4),
            "time_zone": "America/Bogota",
            "duration_minutes": 240,
            "capacity": 100,
            "status": "PUBLISHED",
            "version": 2,
            "created_at": now,
            "updated_at": now,
            "published_at": now,
        }
        with sessionmaker(self.engines["events_w"])() as session, session.begin():
            session.add(EventRow(**values))
        with sessionmaker(self.engines["events_r"])() as session, session.begin():
            session.add(EventReadRow(**values, projected_at=now))
        return str(event_id)

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

    def test_registered_athlete_gets_event_logistics_first(self) -> None:
        """CA1, CA3 y CA4 de punta a punta; SPORTS_MEDICINE y borradores nunca aparecen."""
        provider = self.sign_up("service_provider")
        athlete = self.athlete()
        newcomer = self.athlete()
        soon = (datetime.now(BOGOTA) + timedelta(days=3)).date()
        transport = self.publish(
            provider,
            offer("Bus al evento", "TRANSPORT", local(soon, 6), local(EVENT_DAY, 5)),
        )
        company = self.publish(provider, offer("Liebre", "ACCOMPANIMENT", local(EVENT_DAY, 14)))
        off_day = self.publish(
            provider, offer("Bus otro día", "TRANSPORT", local(EVENT_DAY + timedelta(days=1), 6))
        )
        coaching = self.publish(provider, offer("Técnica de carrera", "COACHING", local(soon, 8)))
        draft = self.publish(
            provider, offer("Sin publicar", "COACHING", local(soon, 9)), publish=False
        )
        medicine = self.publish(
            provider, offer("Valoración médica", "SPORTS_MEDICINE", local(EVENT_DAY, 6))
        )
        event_id = self.add_event(local(EVENT_DAY, 7))
        enrolled = self.mobile.post(
            f"/v1/events/{event_id}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(enrolled.status_code, 201, enrolled.text)
        self.project_outbox()
        self.project_users()

        response = self.mobile.get(SUGGESTIONS, headers=athlete)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            [(item["service_id"], item["reasons"]) for item in body["items"]],
            [
                (transport, ["EVENT_LOGISTICS"]),
                (company, ["EVENT_LOGISTICS"]),
                (coaching, ["UPCOMING_AVAILABILITY"]),
            ],
        )
        returned = {item["service_id"] for item in body["items"]}
        self.assertTrue(returned.isdisjoint({off_day, draft, medicine}))
        first = body["items"][0]
        self.assertEqual(
            (first["title"], first["category"], first["modality"], first["author_name"]),
            ("Bus al evento", "TRANSPORT", "IN_PERSON", "Persona Sintética"),
        )
        self.assertEqual(
            first["fee"], {"amount": "60000.00", "currency": "COP", "billing_unit": "SERVICE"}
        )
        self.assertEqual(
            datetime.fromisoformat(first["next_interval"]["starts_at"]),
            local(EVENT_DAY, 5).astimezone(UTC),
        )
        self.assertEqual(first["next_interval"]["time_zone"], "America/Bogota")
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(
            body["sources"],
            [{"source": "events", "status": "OK"}, {"source": "users", "status": "OK"}],
        )

        # CA2: sin inscripciones, solo entrenamiento y el contexto faltante explicado.
        alone = self.mobile.get(SUGGESTIONS, headers=newcomer).json()
        self.assertEqual([item["service_id"] for item in alone["items"]], [coaching])
        self.assertEqual(
            (alone["personalized"], alone["missing_context"]), (False, ["event_registrations"])
        )

    def test_without_registrations_nor_services_the_list_is_empty_and_explained(self) -> None:
        """CA2: lista vacía con `missing_context`; nunca servicios arbitrarios."""
        provider = self.sign_up("service_provider")
        athlete = self.athlete()
        self.publish(provider, offer("Bus al evento", "TRANSPORT", local(EVENT_DAY, 5)))

        body = self.mobile.get(SUGGESTIONS, headers=athlete).json()

        self.assertEqual(body["items"], [])
        self.assertEqual(body["missing_context"], ["event_registrations"])

    def test_one_source_down_degrades_or_fails_as_declared(self) -> None:
        """CA5 (HA011): Eventos caído → parcial declarado; Servicios caído → `503`/`504`."""
        provider = self.sign_up("service_provider")
        athlete = self.athlete()
        soon = (datetime.now(BOGOTA) + timedelta(days=3)).date()
        coaching = self.publish(provider, offer("Técnica de carrera", "COACHING", local(soon, 8)))
        self.publish(provider, offer("Bus al evento", "TRANSPORT", local(EVENT_DAY, 5)))
        self.enroll_in(athlete, self.add_event(local(EVENT_DAY, 7)))

        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caído")

        def slow(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lento")

        without_events = self.mobile_with(
            events_http=httpx.Client(base_url="http://e", transport=httpx.MockTransport(down)),
            services_http=self.services,
        ).get(SUGGESTIONS, headers=athlete)
        services_down, services_slow = (
            self.mobile_with(
                events_http=self.events,
                services_http=httpx.Client(
                    base_url="http://s", transport=httpx.MockTransport(fail)
                ),
            ).get(SUGGESTIONS, headers=athlete)
            for fail in (down, slow)
        )

        self.assertEqual(without_events.status_code, 200, without_events.text)
        self.assertEqual(
            [item["service_id"] for item in without_events.json()["items"]], [coaching]
        )
        self.assertEqual(
            without_events.json()["sources"][0], {"source": "events", "status": "UNAVAILABLE"}
        )
        self.assertEqual(
            (services_down.status_code, services_down.json()["code"]),
            (503, "upstream_unavailable"),
        )
        self.assertEqual(
            (services_slow.status_code, services_slow.json()["code"]), (504, "upstream_timeout")
        )

    def test_edge_rejects_other_actors(self) -> None:
        organizer = self.sign_up("organizer")
        without_plan = self.sign_up("athlete")

        rejected = self.mobile.get(SUGGESTIONS, headers=organizer)
        no_plan = self.mobile.get(SUGGESTIONS, headers=without_plan)

        self.assertEqual(
            (rejected.status_code, rejected.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (no_plan.status_code, no_plan.json()["code"]), (403, "capability_required")
        )

    def test_p95_below_two_seconds_with_500_services(self) -> None:
        """HA010: 500 servicios publicados (sembrados) y un evento inscrito; p95 < 2 s."""
        athlete = self.athlete()
        self.enroll_in(athlete, self.add_event(local(EVENT_DAY, 7)))
        now = datetime.now(UTC)
        categories = ("COACHING", "TRANSPORT", "ACCOMPANIMENT", "SPORTS_MEDICINE", "NUTRITION")
        services, intervals = [], []
        for index in range(500):
            service_id = uuid.uuid4()
            services.append(
                {
                    "service_id": service_id,
                    "provider_id": uuid.uuid4(),
                    "name": f"Servicio sintético {index:03d}",
                    "description": "Servicio sintético para medir el tiempo de respuesta.",
                    # 300 de las categorías sugeribles y 200 que nunca se sugieren.
                    "category": categories[index % 5],
                    "modality": "HYBRID",
                    "price_amount": Decimal("30000.00"),
                    "currency": "COP",
                    "billing_unit": "SESSION",
                    "status": "PUBLISHED",
                    "version": 2,
                    "created_at": now,
                    "updated_at": now,
                    "published_at": now,
                }
            )
            starts = local(EVENT_DAY + timedelta(days=index % 7 - 3), 6 + index % 12)
            intervals += [
                {
                    "service_id": service_id,
                    "position": position,
                    "starts_at": starts + timedelta(days=position),
                    "ends_at": starts + timedelta(days=position, hours=1),
                    "time_zone": "America/Bogota",
                    "capacity": 5,
                }
                for position in range(2)
            ]
        with sessionmaker(self.engines["services"])() as session, session.begin():
            session.bulk_insert_mappings(ServiceRow, services)  # type: ignore[arg-type]
            session.bulk_insert_mappings(ServiceAvailabilityRow, intervals)  # type: ignore[arg-type]

        durations = []
        for _ in range(20):
            started = time.perf_counter()
            response = self.mobile.get(SUGGESTIONS, params={"limit": "50"}, headers=athlete)
            durations.append(time.perf_counter() - started)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["items"]), 50)

        self.assertLess(statistics.quantiles(durations, n=20)[18], 2.0)

    def enroll_in(self, athlete: dict[str, str], event_id: str) -> None:
        enrolled = self.mobile.post(
            f"/v1/events/{event_id}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(enrolled.status_code, 201, enrolled.text)
        self.project_outbox()


if __name__ == "__main__":
    unittest.main()
