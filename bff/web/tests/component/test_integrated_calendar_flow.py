"""Componente HU018 en web: BFF → Usuarios (sesión y JWKS) + Eventos real (escritura, outbox,
proyector y lectura) + Servicios real, con PostgreSQL migrado (requiere `TEST_DATABASE_URL`).

Mismo montaje que HU024 (`EventFlowCase`) más `complementary-services`. Los eventos, sus
publicaciones, la inscripción del deportista (HU041), los servicios (HU015/HU019) y la
asociación (HU017) se crean con las operaciones reales; la proyección de Eventos se aplica con
su caso de uso, sin Pub/Sub (el transporte lo cubren las pruebas de HU024). Afiliación y las
sesiones planificadas de Entrenamientos se simulan con la forma de sus contratos
(`/v1/subscriptions/by-user/{id}/capabilities` y `/v1/me/training-plan-sessions`).

Trazabilidad: CA1 `test_organizer_*`, `test_service_provider_*` y `test_athlete_*`; CA2 el
borrador publicado aparece una vez con su estado nuevo (`test_organizer_*`); CA3 instantes en
UTC con su `time_zone` (`test_athlete_*`); CA4 borradores y servicios ajenos excluidos; CA5
fuente caída → parcial y todas → `503` (`test_partial_and_total_failure`); CA6 periodo
inválido y autorización (`test_period_and_authorization`).
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_event_publication_flow import ORIGIN, EventFlowCase

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from complementary_services.main import create_app as create_services
from complementary_services.settings import ComplementaryServicesSettings
from events_management.application.messages import parse_sport_event_message
from events_management.application.projections.event_projection import EventsProjector
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

SERVICES = "complementary_services.infrastructure.persistence:migrations"
PATH = "/v1/me/integrated-calendar"
NOW = datetime.now(UTC)
DAY = (NOW + timedelta(days=10)).date()
PERIOD = {
    "from": (NOW + timedelta(days=1)).isoformat(),
    "to": (NOW + timedelta(days=30)).isoformat(),
}
CAPABILITIES = ["calendar.sport.view", "services.sports_medicine.access"]


def capabilities(granted: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    """Afiliación simulada (`/v1/subscriptions/by-user/{id}/capabilities`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"subscription_status": "ACTIVE", "capabilities": granted})

    return handler


def down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("servicio caído")


def offer(name: str, category: str = "SPORTS_MEDICINE", hour: int = 9) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Servicio sintético para el calendario integrado.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "150000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": f"{DAY.isoformat()}T{hour:02d}:00:00-05:00",
                "ends_at": f"{DAY.isoformat()}T{hour + 1:02d}:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 2,
            }
        ],
    }


class PlannedSessions:
    """Entrenamientos simulado con la forma de `GET /v1/me/training-plan-sessions`."""

    def __init__(self) -> None:
        self.session_id, self.plan_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = {
            "plan_id": self.plan_id,
            "plan_title": "Base aeróbica",
            "session_id": self.session_id,
            "scheduled_date": DAY.isoformat(),
            "sport": "running",
            "duration_minutes": 45,
            "rpe": 4,
        }
        inside = request.url.params["from"] <= item["scheduled_date"] <= request.url.params["to"]
        return httpx.Response(200, json={"items": [item] if inside else [], "next_cursor": None})


class IntegratedCalendarFlowTest(EventFlowCase):
    def setUp(self) -> None:
        super().setUp()
        services_url = self.enterContext(temporary_database("hu018_flow_services"))
        upgrade(SERVICES, services_url)
        self.services = self.enterContext(
            TestClient(
                create_services(
                    ComplementaryServicesSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=services_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=self.mock(capabilities(CAPABILITIES)),
                ),
                base_url="http://complementary-services",
            )
        )
        self.sessions = PlannedSessions()
        self.bff = self.calendar_bff()

    def mock(self, handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mock")
        self.addCleanup(client.close)
        return client

    def calendar_bff(
        self,
        *,
        affiliation: httpx.Client | None = None,
        events: httpx.Client | None = None,
        services: httpx.Client | None = None,
        training: httpx.Client | None = None,
    ) -> TestClient:
        return self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                        events_management_base_url="http://events-management",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation or self.mock(capabilities(CAPABILITIES)),
                    events_http=events or self.events,
                    services_http=services or self.services,
                    training_http=training or self.mock(self.sessions),
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

    def project_events(self) -> None:
        store = SqlAlchemyOutboxStore(self.writes)
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads), clock=SystemClock()
        )
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_sport_event_message(json.loads(json.dumps(build_envelope(event)))))
            store.mark_published(event.event_id, "w-test")

    def near_event(self, organizer: dict[str, str], hour: int = 7) -> str:
        return self.draft(organizer, starts_at=f"{DAY.isoformat()}T{hour:02d}:00:00-05:00")

    def published(self, organizer: dict[str, str], hour: int = 7) -> str:
        event_id = self.near_event(organizer, hour)
        response = self.publish(organizer, event_id, str(uuid.uuid4()))
        self.assertEqual(response.status_code, 200, response.text)
        return event_id

    def service(self, provider: dict[str, str], body: dict[str, Any], *, publish: bool) -> str:
        created = self.bff.post(
            "/v1/services", json=body, headers={**provider, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(created.status_code, 201, created.text)
        service_id: str = created.json()["service_id"]
        if publish:
            response = self.bff.post(
                f"/v1/services/{service_id}/publish",
                headers={**provider, "Idempotency-Key": str(uuid.uuid4())},
            )
            self.assertEqual(response.status_code, 200, response.text)
        return service_id

    def calendar(self, user: dict[str, str], client: TestClient | None = None) -> Any:
        return (client or self.bff).get(PATH, params=PERIOD, headers=user)

    def ok(self, response: Any) -> dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body: dict[str, Any] = response.json()
        return body

    def sources(self, body: dict[str, Any]) -> dict[str, str]:
        return {source["name"]: source["status"] for source in body["sources"]}

    def test_organizer_sees_own_drafts_and_publications_once(self) -> None:
        """CA1/CA4: sus eventos `DRAFT` y `PUBLISHED`, nunca los ajenos; CA2: al publicar el
        borrador y proyectarlo aparece una sola vez con el estado nuevo."""
        organizer, other = self.session_for("organizer"), self.session_for("organizer")
        draft, published = self.near_event(organizer, 7), self.published(organizer, 12)
        self.near_event(other, 8)
        self.published(other, 9)
        self.project_events()

        body = self.ok(self.calendar(organizer))

        self.assertEqual(self.sources(body), {"OWN_EVENT": "OK"})
        self.assertEqual(
            [(i["reference"]["event_id"], i["status"]) for i in body["items"]],
            [(draft, "DRAFT"), (published, "PUBLISHED")],
        )
        self.assertNotIn("organizer_id", json.dumps(body))

        response = self.publish(organizer, draft, str(uuid.uuid4()))
        self.assertEqual(response.status_code, 200, response.text)
        self.project_events()
        again = self.ok(self.calendar(organizer))
        self.assertEqual(
            [(i["reference"]["event_id"], i["status"]) for i in again["items"]],
            [(draft, "PUBLISHED"), (published, "PUBLISHED")],
        )

    def test_service_provider_sees_own_availability(self) -> None:
        """CA1/CA4: intervalos de sus servicios `DRAFT` y `PUBLISHED`, no los de otro
        profesional, y el evento publicado del organizador (`EVENT_PUBLISHED`, que Eventos
        admite para el profesional en `GET /v1/events` desde `T-HU018-BE-EVT`)."""
        provider, other = self.session_for("service_provider"), self.session_for("service_provider")
        draft = self.service(provider, offer("Fisioterapia", "COACHING", 8), publish=False)
        published = self.service(provider, offer("Valoración médica", hour=10), publish=True)
        self.service(other, offer("Ajeno", hour=11), publish=True)
        organizer = self.session_for("organizer")
        event_id = self.published(organizer)
        self.project_events()

        body = self.ok(self.calendar(provider))

        self.assertEqual(
            self.sources(body), {"OWN_SERVICE_AVAILABILITY": "OK", "EVENT_PUBLISHED": "OK"}
        )
        self.assertEqual(
            [(i["source"], i["status"], i["reference"]) for i in body["items"]],
            [
                ("EVENT_PUBLISHED", "PUBLISHED", {"event_id": event_id}),
                ("OWN_SERVICE_AVAILABILITY", "DRAFT", {"service_id": draft}),
                ("OWN_SERVICE_AVAILABILITY", "PUBLISHED", {"service_id": published}),
            ],
        )
        self.assertTrue(all(item["occurs_on"] == DAY.isoformat() for item in body["items"]))
        self.assertNotIn("provider_id", json.dumps(body))

    def test_athlete_composes_events_sessions_services_and_registrations(self) -> None:
        """CA1/CA3/CA4: eventos publicados (no borradores), su sesión planificada, la
        disponibilidad del servicio que asoció (no la de otros) y su inscripción, en un orden
        común y con los instantes en UTC con la zona declarada."""
        organizer, provider = self.session_for("organizer"), self.session_for("service_provider")
        event_id = self.published(organizer, 7)
        self.near_event(organizer, 15)  # borrador: nunca visible al deportista
        associated = self.service(provider, offer("Valoración médica", hour=10), publish=True)
        self.service(provider, offer("Otra valoración", hour=11), publish=True)
        athlete = self.session_for("athlete")
        association = self.bff.post(
            "/v1/me/service-associations",
            json={"service_id": associated},
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(association.status_code, 201, association.text)
        registered = self.events.post(
            f"/v1/events/{event_id}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.project_events()

        body = self.ok(self.calendar(athlete))

        self.assertEqual(
            self.sources(body),
            dict.fromkeys(
                (
                    "EVENT_PUBLISHED",
                    "TRAINING_PLAN_SESSION",
                    "ASSOCIATED_SERVICE_AVAILABILITY",
                    "EVENT_REGISTRATION",
                ),
                "OK",
            ),
        )
        self.assertEqual(
            [(i["source"], i["status"]) for i in body["items"]],
            [
                ("TRAINING_PLAN_SESSION", "ASSIGNED"),
                ("EVENT_PUBLISHED", "PUBLISHED"),
                ("EVENT_REGISTRATION", "REGISTERED"),
                ("ASSOCIATED_SERVICE_AVAILABILITY", "AVAILABLE"),
            ],
        )
        session, published, registration, service = body["items"]
        self.assertTrue(all(item["occurs_on"] == DAY.isoformat() for item in body["items"]))
        self.assertEqual(session["reference"]["session_id"], self.sessions.session_id)
        self.assertEqual(published["reference"]["event_id"], event_id)
        self.assertEqual(published["starts_at"], f"{DAY.isoformat()}T12:00:00Z")
        self.assertEqual(published["time_zone"], "America/Bogota")
        self.assertEqual(registration["reference"]["event_id"], event_id)
        self.assertEqual(
            (service["reference"]["service_id"], service["reference"]["association_id"]),
            (associated, association.json()["association_id"]),
        )
        self.assertEqual(service["starts_at"], f"{DAY.isoformat()}T15:00:00Z")
        self.assertNotIn("athlete_id", json.dumps(body))

    def test_partial_and_total_failure(self) -> None:
        """CA5: con Entrenamientos caído el deportista recibe las demás fuentes y la sesión se
        declara incompleta; con todos los dominios caídos, `503` trazable."""
        organizer, athlete = self.session_for("organizer"), self.session_for("athlete")
        event_id = self.published(organizer)
        self.project_events()

        partial = self.ok(self.calendar(athlete, self.calendar_bff(training=self.mock(down))))
        self.assertEqual(self.sources(partial)["TRAINING_PLAN_SESSION"], "UNAVAILABLE")
        self.assertEqual(self.sources(partial)["EVENT_PUBLISHED"], "OK")
        self.assertEqual([i["reference"].get("event_id") for i in partial["items"]], [event_id])

        failing = self.calendar_bff(
            events=self.mock(down), services=self.mock(down), training=self.mock(down)
        )
        for user in (athlete, organizer):
            response = self.calendar(user, failing)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["code"], "upstream_unavailable")
            self.assertTrue(response.json()["correlation_id"])

    def test_period_and_authorization(self) -> None:
        """CA6: periodo mayor de 92 días → `422`; sin token `401`; deportista sin
        `calendar.sport.view` → `403 capability_required`."""
        organizer = self.session_for("organizer")
        too_long = self.bff.get(
            PATH,
            params={"from": "2099-01-01T00:00:00Z", "to": "2099-04-03T00:00:01Z"},
            headers=organizer,
        )
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(
            too_long.json()["details"], [{"field": "query.to", "issue": "out_of_range"}]
        )
        self.assertEqual(self.bff.get(PATH, params=PERIOD).status_code, 401)
        athlete = self.session_for("athlete")
        without = self.calendar(athlete, self.calendar_bff(affiliation=self.mock(capabilities([]))))
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
