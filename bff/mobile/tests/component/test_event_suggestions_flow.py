"""Componente HU027 (ADR-041): `bff-mobile` → Afiliación (capacidad `events.suggest`) →
`events-management` (`GET /v1/events`, que vuelve a verificar `calendar.sport.view`, y el detalle
con la inscripción propia) + Usuarios (`athlete-context`), todos reales con PostgreSQL migrado
(requiere `TEST_DATABASE_URL`).

Mismo montaje que `test_event_registration_flow.py` (se importa el módulo, no la clase, para no
volver a descubrir sus pruebas). Los eventos `PUBLISHED` se siembran como en HU041 y HU029; la
inscripción es la real de HU041 y se proyecta sin Pub/Sub. El perfil (HU005 y HU006) se registra
con las operaciones reales de Usuarios y se proyecta sin Pub/Sub.
"""

import base64
import json
import statistics
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.infrastructure.persistence_write.models import EventRow
from events_management.main import create_app as create_events
from events_management.settings import EventsManagementSettings
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

SUGGESTIONS = "/v1/me/suggestions/events"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
CATALOG = "plans-2026-10-13.v1"


def down(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("caído")


def slow(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("lento")


def event_values(
    discipline: str, city: str, starts_at: datetime, **overrides: Any
) -> dict[str, Any]:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "organizer_id": uuid.uuid4(),
        "name": f"{discipline} en {city}",
        "description": "Evento sintético para las sugerencias.",
        "discipline": discipline,
        "location_name": "Parque principal",
        "location_address": "Calle 1 # 2-3",
        "location_city": city,
        "location_country_code": "CO",
        "starts_at": starts_at,
        "ends_at": starts_at + timedelta(hours=3),
        "time_zone": "America/Bogota",
        "duration_minutes": 180,
        "capacity": 120,
        "status": "PUBLISHED",
        "version": 2,
        "created_at": now,
        "updated_at": now,
        "published_at": now,
    }
    values.update(overrides)
    if values["status"] != "PUBLISHED":
        values["published_at"] = None
    return values


class EventSuggestionsFlowTest(unittest.TestCase):
    sign_up = registration_flow.EventRegistrationFlowTest.sign_up
    client = registration_flow.EventRegistrationFlowTest.client
    project_outbox = registration_flow.EventRegistrationFlowTest.project_outbox

    def setUp(self) -> None:
        registration_flow.EventRegistrationFlowTest.setUp(self)  # type: ignore[arg-type]
        affiliation_url = self.enterContext(temporary_database("hu027_affiliation"))
        upgrade(AFFILIATION, affiliation_url)
        self.engines["affiliation"] = create_engine(affiliation_url)
        self.addCleanup(self.engines["affiliation"].dispose)
        self.affiliation = self.client(
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
        # `GET /v1/events` vuelve a verificar `calendar.sport.view` del deportista en Afiliación.
        self.events = self.client(
            create_events(
                EventsManagementSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["events_w"],  # type: ignore[arg-type]
                    database_read_url=self.urls["events_r"],  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                ),
                users_http=self.users,
                affiliation_http=self.affiliation,
            ),
            "http://events-management",
        )
        self.mobile = self.mobile_with(events_http=self.events, users_http=self.users)

    def mobile_with(self, *, events_http: httpx.Client, users_http: httpx.Client) -> TestClient:
        unused = httpx.Client(
            base_url="http://unused", transport=httpx.MockTransport(registration_flow.unused)
        )
        return self.client(
            create_bff(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events-management",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=users_http,
                affiliation_http=self.affiliation,
                training_http=unused,
                events_http=events_http,
                services_http=unused,
            ),
            "https://bff-mobile",
        )

    def athlete(self) -> dict[str, str]:
        """Deportista con su plan `FREE` (incluye `events.suggest` y `calendar.sport.view`)."""
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

    def profile(self, athlete: dict[str, str], *, city: str, sport: str) -> None:
        """HU005 (ciudad) y HU006 (deportes) con las operaciones reales de Usuarios."""
        for path, body in (
            (
                "/v1/me/demographic-profile",
                {
                    "birth_date": "1991-07-02",
                    "gender": "MALE",
                    "country_code": "CO",
                    "city": city,
                    "marital_status": "SINGLE",
                    "occupation": "Docente",
                    "income_range": "2_4_SMMLV",
                },
            ),
            (
                "/v1/me/sport-habits",
                {
                    "sports": [
                        {
                            "sport": sport,
                            "frequency_per_week": 3,
                            "session_duration_minutes": 60,
                            "intensity": "MODERATE",
                            "level": "INTERMEDIATE",
                        }
                    ],
                    "availability": {"days": ["SUN"], "time_slots": ["MORNING"]},
                    "primary_goal": "HEALTH",
                },
            ),
        ):
            response = self.users.put(
                path, json=body, headers={**athlete, "Idempotency-Key": str(uuid.uuid4())}
            )
            self.assertEqual(response.status_code, 201, response.text)
        self.project_users()

    def project_users(self) -> None:
        """`usuarios-outbox-publisher` + `usuarios-read-projector` sin Pub/Sub."""
        outbox = UsersOutboxStore(sessionmaker(self.engines["users_w"]))
        project = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(sessionmaker(self.engines["users_r"])),
            clock=UsersClock(),
        )
        for event in outbox.claim_batch("w-test", 100, 30):
            project(parse_users_event(json.loads(json.dumps(users_envelope(event)))))
            outbox.mark_published(event.event_id, "w-test")

    def seed(self, *rows: dict[str, Any]) -> list[str]:
        """Eventos sintéticos en escritura y lectura (como en HU041)."""
        now = datetime.now(UTC)
        with sessionmaker(self.engines["events_w"])() as session, session.begin():
            session.bulk_insert_mappings(EventRow, list(rows))  # type: ignore[arg-type]
        with sessionmaker(self.engines["events_r"])() as session, session.begin():
            session.bulk_insert_mappings(
                EventReadRow,  # type: ignore[arg-type]
                [{**row, "projected_at": now} for row in rows],
            )
        return [str(row["event_id"]) for row in rows]

    def test_personalized_suggestions_from_published_events(self) -> None:
        """CA1, CA3 y CA4 de punta a punta: orden, motivos, tarifa, cupo e inscripción."""
        athlete = self.athlete()
        self.profile(athlete, city="Bogotá", sport="swimming")
        now = datetime.now(UTC)
        fee = {"fee_amount": Decimal("85000.00"), "fee_currency": "COP", "fee_updated_at": now}
        both, sport, city, other, draft, started, far = self.seed(
            event_values("Natación", "Bogotá", now + timedelta(days=40), capacity=300, **fee),
            event_values("Aguas abiertas", "Cartagena", now + timedelta(days=3)),
            event_values("Carrera", "bogota", now + timedelta(days=2)),
            event_values("Ajedrez", "Cali", now + timedelta(days=1)),
            event_values("Natación", "Bogotá", now + timedelta(days=5), status="DRAFT", version=1),
            event_values("Natación", "Bogotá", now - timedelta(hours=1)),
            event_values("Natación", "Bogotá", now + timedelta(days=95)),
        )
        enrolled = self.mobile.post(
            f"/v1/events/{city}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(enrolled.status_code, 201, enrolled.text)
        self.project_outbox()

        response = self.mobile.get(SUGGESTIONS, headers=athlete)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            [(item["event_id"], item["reasons"]) for item in body["items"]],
            [
                (both, ["SPORT_MATCH", "CITY_MATCH"]),
                (sport, ["SPORT_MATCH"]),
                (city, ["CITY_MATCH"]),
                (other, ["UPCOMING"]),
            ],
        )
        self.assertTrue({draft, started, far}.isdisjoint(i["event_id"] for i in body["items"]))
        first, _, registered, _ = body["items"]
        self.assertEqual(
            (first["title"], first["capacity"], first["fee"], first["my_registration"]),
            ("Natación en Bogotá", 300, {"amount": "85000.00", "currency": "COP"}, None),
        )
        self.assertEqual(
            (first["location"]["city"], first["time_zone"]), ("Bogotá", "America/Bogota")
        )
        self.assertNotIn("organizer_id", first)
        self.assertEqual(
            registered["my_registration"]["registration_id"], enrolled.json()["registration_id"]
        )
        self.assertIsNone(registered["fee"])
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])

    def test_without_profile_or_users_down_is_not_personalized(self) -> None:
        """CA2 (HA011): sin perfil o con Usuarios caído, próximos eventos por fecha."""
        athlete = self.athlete()
        now = datetime.now(UTC)
        later, sooner = self.seed(
            event_values("Natación", "Bogotá", now + timedelta(days=9)),
            event_values("Ciclismo", "Cali", now + timedelta(days=4)),
        )
        users = self.users

        def users_down(request: httpx.Request) -> httpx.Response:
            # Solo responde el JWKS: el contexto del deportista no está disponible.
            if request.url.path == "/.well-known/jwks.json":
                return users.get(request.url.path)
            raise httpx.ConnectError("usuarios caído")

        without_profile = self.mobile.get(SUGGESTIONS, headers=athlete).json()
        without_users = self.mobile_with(
            events_http=self.events,
            users_http=httpx.Client(
                base_url="http://users-management", transport=httpx.MockTransport(users_down)
            ),
        ).get(SUGGESTIONS, headers=athlete)

        for body in (without_profile, without_users.json()):
            self.assertEqual([item["event_id"] for item in body["items"]], [sooner, later])
            self.assertTrue(all(item["reasons"] == ["UPCOMING"] for item in body["items"]))
            self.assertEqual(
                (body["personalized"], body["missing_context"]), (False, ["sports", "city"])
            )
        self.assertEqual(without_profile["sources"], [{"source": "users", "status": "OK"}])
        self.assertEqual(
            without_users.json()["sources"], [{"source": "users", "status": "UNAVAILABLE"}]
        )

    def test_events_down_is_503_or_504(self) -> None:
        """CA5 (HA011): sin Eventos no hay lista: `503`/`504`."""
        athlete = self.athlete()

        unavailable, timeout = (
            self.mobile_with(
                events_http=httpx.Client(base_url="http://e", transport=httpx.MockTransport(fail)),
                users_http=self.users,
            ).get(SUGGESTIONS, headers=athlete)
            for fail in (down, slow)
        )

        self.assertEqual(
            (unavailable.status_code, unavailable.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))

    def test_edge_rejects_other_actors_and_athletes_without_plan(self) -> None:
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

    def test_p95_below_two_seconds_with_1000_published_events(self) -> None:
        """HA010: 1.000 eventos publicados en los próximos 90 días (sembrados); p95 < 2 s con la
        página máxima de 50 sugerencias (50 detalles)."""
        athlete = self.athlete()
        self.profile(athlete, city="Medellín", sport="running")
        now = datetime.now(UTC)
        disciplines = ("Carrera", "Trail", "Ciclismo", "Natación", "Fuerza")
        cities = ("Medellín", "Bogotá", "Cali", "Barranquilla")
        self.seed(
            *(
                event_values(
                    disciplines[index % 5],
                    cities[index % 4],
                    now + timedelta(days=1, minutes=index * 125),
                )
                for index in range(1000)
            )
        )

        durations = []
        for _ in range(20):
            started = time.perf_counter()
            response = self.mobile.get(SUGGESTIONS, params={"limit": "50"}, headers=athlete)
            durations.append(time.perf_counter() - started)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(len(body["items"]), 50)
            self.assertEqual(body["items"][0]["reasons"], ["SPORT_MATCH", "CITY_MATCH"])

        self.assertLess(statistics.quantiles(durations, n=20)[18], 2.0)


if __name__ == "__main__":
    unittest.main()
