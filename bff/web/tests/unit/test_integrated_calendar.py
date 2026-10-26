"""BFF web, HU018: `GET /v1/me/integrated-calendar` (ADR-039 con ADR-031) contra Usuarios
(JWKS), Afiliación, Eventos, Servicios y Entrenamientos simulados con `httpx.MockTransport`
y la forma de sus contratos.

Trazabilidad: CA1 matriz actor-fuentes y referencias (`test_*_sources`); CA2 el BFF no
espera ni duplica (`test_each_source_is_queried_once_without_waiting`); CA3 instante, zona y
`occurs_on` (`test_athlete_sources`); CA4 visibilidad (`test_organizer_sources`,
`test_service_provider_sources`, `test_associated_services_without_capability_are_empty`);
CA5 parcial y caída total (`test_partial_*`, `test_all_sources_down_*`); CA6 periodo y
`truncated` (`test_invalid_period`, `test_truncated_*`).
"""

import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.api.integrated_calendar import build_integrated_calendar_router
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.events_management import EventsManagementClient
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.correlation import CORRELATION_HEADER
from sportapp_platform.tokens import VerifiedAccessToken

PATH = "/v1/me/integrated-calendar"
PERIOD = {"from": "2026-11-01T00:00:00-05:00", "to": "2026-11-30T23:59:59-05:00"}
LOCATION = {"name": "Parque", "address": "Calle 1", "city": "Bogotá", "country_code": "CO"}
MY_EVENTS, EVENTS, REGISTRATIONS = (
    "/v1/me/events",
    "/v1/events",
    "/v1/me/event-registrations",
)
MY_SERVICES, ASSOCIATIONS, AVAILABILITY = (
    "/v1/me/services",
    "/v1/me/service-associations",
    "/v1/services/availability",
)
SESSIONS = "/v1/me/training-plan-sessions"
APPOINTMENTS = "/v1/me/appointments"


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def at(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def event(starts_at: datetime, *, hours: int = 2, status: str = "PUBLISHED") -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "organizer_id": str(uuid.uuid4()),
        "name": f"Evento {status.lower()} {starts_at:%d}",
        "description": "Evento sintético.",
        "discipline": "Running",
        "location": LOCATION,
        "starts_at": iso(starts_at),
        "ends_at": iso(starts_at + timedelta(hours=hours)),
        "time_zone": "America/Bogota",
        "status": status,
        "version": 1,
        "fee": None,
    }


def registration(starts_at: datetime) -> dict[str, Any]:
    published = event(starts_at)
    return {
        "registration_id": str(uuid.uuid4()),
        "event_id": published["event_id"],
        "status": "REGISTERED",
        "registered_at": iso(at(1)),
        "applied_fee": None,
        "version": 1,
        "event": {
            key: published[key] for key in ("name", "starts_at", "ends_at", "time_zone", "location")
        },
    }


def interval(starts_at: datetime) -> dict[str, Any]:
    return {
        "starts_at": iso(starts_at),
        "ends_at": iso(starts_at + timedelta(hours=1)),
        "time_zone": "America/Bogota",
        "capacity": 3,
    }


def service(
    *starts: datetime, status: str = "PUBLISHED", category: str = "SPORTS_MEDICINE"
) -> dict[str, Any]:
    return {
        "service_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()),
        "name": f"Servicio {status.lower()}",
        "description": "Servicio sintético.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "100000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [interval(start) for start in starts],
        "status": status,
        "available": True,
    }


def association(offer: dict[str, Any]) -> dict[str, Any]:
    return {
        "association_id": str(uuid.uuid4()),
        "service_id": offer["service_id"],
        "service": {
            "name": offer["name"],
            "category": offer["category"],
            "modality": offer["modality"],
            "fee": offer["price"],
        },
        "status": "ACTIVE",
        "associated_at": iso(at(1)),
        "ended_at": None,
        "version": 1,
    }


def session(day: date) -> dict[str, Any]:
    return {
        "plan_id": str(uuid.uuid4()),
        "plan_title": "Base aeróbica",
        "session_id": str(uuid.uuid4()),
        "scheduled_date": day.isoformat(),
        "sport": "running",
        "duration_minutes": 45,
        "rpe": 4,
    }


def appointment(starts_at: datetime, *, status: str = "BOOKED") -> dict[str, Any]:
    """Cita de HU040 tal como la responde `complementary-services` 0.6.0."""
    return {
        "appointment_id": str(uuid.uuid4()),
        "athlete_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()),
        "service_id": str(uuid.uuid4()),
        "availability_id": str(uuid.uuid4()),
        "service": {
            "name": "Entrenamiento virtual",
            "category": "COACHING",
            "modality": "VIRTUAL",
            "fee": {"amount": "80000.00", "currency": "COP", "billing_unit": "SESSION"},
        },
        "starts_at": iso(starts_at),
        "ends_at": iso(starts_at + timedelta(hours=1)),
        "time_zone": "America/Bogota",
        "status": status,
        "meeting_url": None,
        "booked_at": iso(at(1)),
        "cancelled_at": iso(at(2)) if status != "BOOKED" else None,
        "version": 1 if status == "BOOKED" else 2,
    }


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


def _in_period(query: dict[str, Any], start: str, end: str) -> bool:
    return start <= iso(datetime.fromisoformat(query["to"])) and end >= iso(
        datetime.fromisoformat(query["from"])
    )


class Domains:
    """Los tres dominios con paginación por desplazamiento y los filtros de sus contratos."""

    def __init__(self) -> None:
        self.data: dict[str, list[dict[str, Any]]] = {
            path: []
            for path in (
                MY_EVENTS,
                EVENTS,
                REGISTRATIONS,
                MY_SERVICES,
                ASSOCIATIONS,
                AVAILABILITY,
                SESSIONS,
                APPOINTMENTS,
            )
        }
        self.requests: list[httpx.Request] = []
        self.status: dict[str, tuple[int, dict[str, Any]]] = {}
        self.failure: dict[str, type[Exception]] = {}
        self.payload: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.failure:
            raise self.failure[path]("caído")
        if path in self.status:
            status, body = self.status[path]
            return httpx.Response(status, json=body)
        if path in self.payload:
            return httpx.Response(200, json=self.payload[path])
        query = dict(request.url.params)
        items = [item for item in self.data[path] if self._matches(path, request, query, item)]
        offset, limit = int(query.get("cursor", "0")), int(query.get("limit", "20"))
        following = offset + limit
        return httpx.Response(
            200,
            json={
                "items": items[offset:following],
                "next_cursor": str(following) if following < len(items) else None,
            },
        )

    @staticmethod
    def _matches(
        path: str, request: httpx.Request, query: dict[str, str], item: dict[str, Any]
    ) -> bool:
        if path == EVENTS:
            return _in_period(query, item["starts_at"], item["ends_at"])
        if path == SESSIONS:
            return query["from"] <= item["scheduled_date"] <= query["to"]
        if path == APPOINTMENTS:
            if query.get("status", item["status"]) != item["status"]:
                return False
            start = datetime.fromisoformat(item["starts_at"])
            return (
                datetime.fromisoformat(query["from"]) <= start < datetime.fromisoformat(query["to"])
            )
        if path == AVAILABILITY:
            categories = request.url.params.get_list("category")
            return item["category"] in categories
        return True

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def queries(self, path: str) -> list[httpx.QueryParams]:
        return [request.url.params for request in self.requests if request.url.path == path]


class AvailabilityCatalog(Domains):
    """El catálogo de HU020 recorta los intervalos a los que empiezan en `[from, to)`."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        if request.url.path != AVAILABILITY or response.status_code != 200:
            return response
        body = response.json()
        window_from = datetime.fromisoformat(request.url.params["from"])
        window_to = datetime.fromisoformat(request.url.params["to"])
        body["items"] = [
            {
                **offer,
                "availability": [
                    slot
                    for slot in offer["availability"]
                    if window_from <= datetime.fromisoformat(slot["starts_at"]) < window_to
                ],
            }
            for offer in body["items"]
        ]
        body["items"] = [offer for offer in body["items"] if offer["availability"]]
        body["restricted_categories"] = []
        return httpx.Response(200, json=body)


class IntegratedCalendarRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.domains = AvailabilityCatalog()
        self.capabilities: dict[str, Any] = {
            "subscription_status": "ACTIVE",
            "capabilities": ["calendar.sport.view"],
        }
        self.affiliation_failure: type[Exception] | None = None
        self.affiliation_calls = 0

        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
        )

        def transport(base_url: str) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(self.domains), base_url=base_url)

        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    transport=httpx.MockTransport(Upstreams().users), base_url="http://users"
                ),
                affiliation_http=httpx.Client(
                    transport=httpx.MockTransport(self.affiliation), base_url="http://affiliation"
                ),
                events_http=transport("http://events"),
                services_http=transport("http://services"),
                training_http=transport("http://training"),
            )
        )

    def affiliation(self, request: httpx.Request) -> httpx.Response:
        self.affiliation_calls += 1
        if self.affiliation_failure is not None:
            raise self.affiliation_failure("afiliación")
        return httpx.Response(200, json=self.capabilities)

    def calendar(
        self, actor: str | None, params: dict[str, str] | None = None, **headers: str
    ) -> Any:
        return self.client.get(
            PATH,
            params=PERIOD if params is None else params,
            headers={"Authorization": f"Bearer {token(actor)}", **headers},
        )

    def assert_ok(self, response: Any) -> dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body: dict[str, Any] = response.json()
        return body

    def sources(self, body: dict[str, Any]) -> list[tuple[str, str, bool]]:
        return [(s["name"], s["status"], s["truncated"]) for s in body["sources"]]

    # ------------------------------------------------------------- matriz actor-fuentes

    def test_organizer_sources(self) -> None:
        """CA1/CA4: sus `DRAFT` y `PUBLISHED` del periodo; otro estado no aparece; la
        lectura se detiene en el primer evento que empieza después del periodo."""
        draft, published = event(at(10), status="DRAFT"), event(at(12))
        before = [event(at(1, month=10) + timedelta(hours=n)) for n in range(98)]
        cancelled = event(at(11), status="CANCELLED")
        later = [event(at(1, month=12) + timedelta(days=n)) for n in range(150)]
        # Primera página: 98 anteriores, el borrador y el cancelado; la segunda corta en
        # el primer evento posterior al periodo.
        self.domains.data[MY_EVENTS] = [*before, draft, cancelled, published, *later]

        body = self.assert_ok(self.calendar("organizer"))

        self.assertEqual(self.sources(body), [("OWN_EVENT", "OK", False)])
        self.assertEqual(
            [(i["reference"]["event_id"], i["status"]) for i in body["items"]],
            [(draft["event_id"], "DRAFT"), (published["event_id"], "PUBLISHED")],
        )
        self.assertNotIn("organizer_id", str(body))
        self.assertEqual(set(self.domains.paths()), {MY_EVENTS})
        self.assertEqual(
            [dict(q) for q in self.domains.queries(MY_EVENTS)],
            [{"limit": "100"}, {"limit": "100", "cursor": "100"}],
        )
        self.assertEqual(
            (body["from"], body["to"]), ("2026-11-01T05:00:00Z", "2026-12-01T04:59:59Z")
        )
        self.assertTrue(body["evaluated_at"].endswith("Z"))

    def test_service_provider_sources(self) -> None:
        """CA1/CA4: intervalos de sus servicios `DRAFT` y `PUBLISHED` del periodo y los
        eventos publicados, recorriendo todas las páginas de sus servicios."""
        draft = service(at(5, 9), at(10, 9, month=12), status="DRAFT")
        published = service(at(6, 9))
        fillers = [service(at(25, 9, month=10)) for _ in range(120)]
        self.domains.data[MY_SERVICES] = [*fillers, draft, published]
        publicado = event(at(7))
        self.domains.data[EVENTS] = [publicado]

        body = self.assert_ok(self.calendar("service_provider"))

        self.assertEqual(
            self.sources(body),
            [
                ("OWN_SERVICE_AVAILABILITY", "OK", False),
                ("EVENT_PUBLISHED", "OK", False),
                ("OWN_APPOINTMENT", "OK", False),
            ],
        )
        self.assertEqual(
            [(i["source"], i["status"], i["reference"]) for i in body["items"]],
            [
                ("OWN_SERVICE_AVAILABILITY", "DRAFT", {"service_id": draft["service_id"]}),
                ("OWN_SERVICE_AVAILABILITY", "PUBLISHED", {"service_id": published["service_id"]}),
                ("EVENT_PUBLISHED", "PUBLISHED", {"event_id": publicado["event_id"]}),
            ],
        )
        self.assertEqual(body["items"][0]["capacity"], 3)
        self.assertNotIn("provider_id", str(body))
        self.assertEqual(len(self.domains.queries(MY_SERVICES)), 2)
        [events_query] = self.domains.queries(EVENTS)
        self.assertEqual(
            dict(events_query),
            {"from": "2026-11-01T05:00:00Z", "to": "2026-12-01T04:59:59Z", "limit": "200"},
        )
        self.assertEqual(self.affiliation_calls, 0)

    def test_service_provider_without_event_access_gets_a_partial_calendar(self) -> None:
        """Eventos aún no admite al profesional en `GET /v1/events` (`T-HU018-BE-EVT`): la
        fuente se declara `UNAVAILABLE` y el resto se entrega (CA5)."""
        self.domains.data[MY_SERVICES] = [service(at(6, 9))]
        self.domains.status[EVENTS] = (403, error("permission_required"))

        body = self.assert_ok(self.calendar("service_provider"))

        self.assertEqual(
            self.sources(body),
            [
                ("OWN_SERVICE_AVAILABILITY", "OK", False),
                ("EVENT_PUBLISHED", "UNAVAILABLE", False),
                ("OWN_APPOINTMENT", "OK", False),
            ],
        )
        self.assertEqual([i["source"] for i in body["items"]], ["OWN_SERVICE_AVAILABILITY"])

    def test_service_provider_appointments(self) -> None:
        """HU040 (ADR-046 §4): `OWN_APPOINTMENT` con las citas `BOOKED` de sus servicios; una
        cancelada no aparece (ADR-039, CA4) y la que empezó el día anterior y cruza el periodo
        sí; sin datos del deportista; ningún otro actor recibe la fuente."""
        booked = appointment(at(9, 15))
        cancelled = appointment(at(9, 16), status="CANCELLED_BY_ATHLETE")
        crossing = {**appointment(at(1, 4)), "ends_at": iso(at(1, 6))}
        outside = appointment(at(2, 12, month=12))
        self.domains.data[APPOINTMENTS] = [crossing, booked, cancelled, outside]

        body = self.assert_ok(self.calendar("service_provider"))

        own = [i for i in body["items"] if i["source"] == "OWN_APPOINTMENT"]
        self.assertEqual(
            [(i["reference"]["appointment_id"], i["status"]) for i in own],
            [(crossing["appointment_id"], "BOOKED"), (booked["appointment_id"], "BOOKED")],
        )
        first = own[1]
        self.assertEqual(
            {k: first[k] for k in ("item_id", "occurs_on", "title", "category", "modality")},
            {
                "item_id": f"OWN_APPOINTMENT:{booked['appointment_id']}",
                "occurs_on": "2026-11-09",
                "title": "Entrenamiento virtual",
                "category": "COACHING",
                "modality": "VIRTUAL",
            },
        )
        self.assertEqual(
            first["reference"],
            {
                "appointment_id": booked["appointment_id"],
                "service_id": booked["service_id"],
                "availability_id": booked["availability_id"],
            },
        )
        for field in ("meeting_url", "athlete_id", "athlete_display_name"):
            self.assertNotIn(field, first)
        [query] = self.domains.queries(APPOINTMENTS)
        self.assertEqual(
            dict(query),
            {
                "from": "2026-10-31T05:00:00Z",
                "to": "2026-12-01T05:00:00Z",
                "status": "BOOKED",
                "limit": "100",
            },
        )
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                other = self.assert_ok(self.calendar(actor))
                self.assertNotIn("OWN_APPOINTMENT", [s["name"] for s in other["sources"]])

    def test_service_provider_appointments_source_down(self) -> None:
        """CA5 de HU018: Servicios caído para las citas → `UNAVAILABLE`, el resto se entrega."""
        self.domains.data[MY_SERVICES] = [service(at(6, 9))]
        self.domains.failure[APPOINTMENTS] = httpx.ConnectError

        body = self.assert_ok(self.calendar("service_provider"))

        self.assertIn(("OWN_APPOINTMENT", "UNAVAILABLE", False), self.sources(body))
        self.assertIn("OWN_SERVICE_AVAILABILITY", [i["source"] for i in body["items"]])

    def test_athlete_sources(self) -> None:
        """CA1/CA3: las cuatro fuentes del deportista en un orden común; la sesión sin hora va
        primero en su día y el evento de las 21:00 en Bogotá ocurre ese día aunque en UTC sea
        el siguiente; solo aparecen los servicios asociados."""
        night = event(datetime(2026, 11, 16, 2, tzinfo=UTC))  # 21:00 del 15 en Bogotá
        self.domains.data[EVENTS] = [night, event(at(20))]
        self.domains.data[SESSIONS] = [session(date(2026, 11, 15)), session(date(2026, 12, 2))]
        associated, other = service(at(15, 14)), service(at(15, 15))
        self.domains.data[ASSOCIATIONS] = [association(associated)]
        self.domains.data[AVAILABILITY] = [associated, other]
        mine = registration(at(20))
        self.domains.data[REGISTRATIONS] = [registration(at(10, month=10)), mine]

        body = self.assert_ok(self.calendar("athlete"))

        self.assertEqual(
            self.sources(body),
            [
                ("EVENT_PUBLISHED", "OK", False),
                ("TRAINING_PLAN_SESSION", "OK", False),
                ("ASSOCIATED_SERVICE_AVAILABILITY", "OK", False),
                ("EVENT_REGISTRATION", "OK", False),
            ],
        )
        self.assertEqual(
            [(i["occurs_on"], i["source"]) for i in body["items"]],
            [
                ("2026-11-15", "TRAINING_PLAN_SESSION"),
                ("2026-11-15", "ASSOCIATED_SERVICE_AVAILABILITY"),
                ("2026-11-15", "EVENT_PUBLISHED"),
                ("2026-11-20", "EVENT_PUBLISHED"),
                ("2026-11-20", "EVENT_REGISTRATION"),
            ],
        )
        published_night = body["items"][2]
        self.assertEqual(
            (published_night["starts_at"], published_night["time_zone"]),
            ("2026-11-16T02:00:00Z", "America/Bogota"),
        )
        self.assertEqual(body["items"][1]["reference"]["service_id"], associated["service_id"])
        self.assertEqual(body["items"][1]["status"], "AVAILABLE")
        self.assertEqual(body["items"][4]["reference"]["registration_id"], mine["registration_id"])
        self.assertEqual(self.affiliation_calls, 1)
        [sessions_query] = self.domains.queries(SESSIONS)
        self.assertEqual(
            dict(sessions_query), {"from": "2026-11-01", "to": "2026-11-30", "limit": "200"}
        )
        [catalog_query] = self.domains.queries(AVAILABILITY)
        self.assertEqual(catalog_query.get_list("category"), ["SPORTS_MEDICINE"])
        self.assertEqual(catalog_query["availability"], "available")

    def test_athlete_catalog_is_read_in_windows_of_31_days(self) -> None:
        """El catálogo admite 31 días por consulta: un periodo de 92 días usa tres ventanas
        contiguas y un intervalo aparece una sola vez."""
        offer = service(
            at(1, 9, month=12), at(15, 9, month=12), at(20, 9, month=1).replace(year=2027)
        )
        self.domains.data[ASSOCIATIONS] = [association(offer)]
        self.domains.data[AVAILABILITY] = [offer]
        period = {"from": "2026-11-01T00:00:00Z", "to": "2027-02-01T00:00:00Z"}

        body = self.assert_ok(self.calendar("athlete", period))

        windows = [(q["from"], q["to"]) for q in self.domains.queries(AVAILABILITY)]
        self.assertEqual(
            windows,
            [
                ("2026-11-01T00:00:00Z", "2026-12-02T00:00:00Z"),
                ("2026-12-02T00:00:00Z", "2027-01-02T00:00:00Z"),
                ("2027-01-02T00:00:00Z", "2027-02-01T00:00:00Z"),
            ],
        )
        associated = [i for i in body["items"] if i["source"] == "ASSOCIATED_SERVICE_AVAILABILITY"]
        self.assertEqual(
            [i["starts_at"] for i in associated],
            ["2026-12-01T09:00:00Z", "2026-12-15T09:00:00Z", "2027-01-20T09:00:00Z"],
        )

    def test_associated_services_without_capability_are_empty(self) -> None:
        """CA4: sin la capacidad médica no hay servicios asociados autorizados; la fuente queda
        vacía y completa, sin consultar el catálogo. Sin asociaciones, tampoco."""
        for status in ((403, error("capability_required")), None):
            with self.subTest(status=status):
                self.domains.requests.clear()
                if status is None:
                    self.domains.status.pop(ASSOCIATIONS)
                else:
                    self.domains.status[ASSOCIATIONS] = status
                body = self.assert_ok(self.calendar("athlete"))
                self.assertIn(("ASSOCIATED_SERVICE_AVAILABILITY", "OK", False), self.sources(body))
                self.assertNotIn(AVAILABILITY, self.domains.paths())

    def test_each_source_is_queried_once_without_waiting(self) -> None:
        """CA2: cada fuente se consulta una vez con el token y la correlación; el BFF no
        reintenta ni espera proyecciones."""
        correlation = str(uuid.uuid4())
        self.domains.data[ASSOCIATIONS] = [association(service(at(3)))]
        self.assert_ok(self.calendar("athlete", **{CORRELATION_HEADER: correlation}))
        self.assertEqual(
            sorted(self.domains.paths()),
            sorted([EVENTS, SESSIONS, ASSOCIATIONS, AVAILABILITY, REGISTRATIONS]),
        )
        for request in self.domains.requests:
            self.assertTrue(request.headers["Authorization"].startswith("Bearer "))
            self.assertEqual(request.headers[CORRELATION_HEADER], correlation)

    # ------------------------------------------------------------ autorización y periodo

    def test_authentication_and_actor(self) -> None:
        without = self.client.get(PATH, params=PERIOD)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (401, "invalid_access_token")
        )
        for actor in (None, "administrator"):
            with self.subTest(actor=actor):
                response = self.calendar(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.domains.requests, [])

    def test_athlete_capability(self) -> None:
        """`calendar.sport.view` vigente; sin ella `403`, sin Afiliación `503`/`504`."""
        cases = [
            (
                {"subscription_status": "ACTIVE", "capabilities": []},
                None,
                403,
                "capability_required",
            ),
            (
                {"subscription_status": "NONE", "capabilities": ["calendar.sport.view"]},
                None,
                403,
                "capability_required",
            ),
            (self.capabilities, httpx.ConnectError, 503, "upstream_unavailable"),
            (self.capabilities, httpx.ReadTimeout, 504, "upstream_timeout"),
        ]
        for snapshot, failure, status, code in cases:
            with self.subTest(code=code, failure=failure):
                self.capabilities, self.affiliation_failure = snapshot, failure
                response = self.calendar("athlete")
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.assertEqual(self.domains.requests, [])

    def test_invalid_period(self) -> None:
        """CA6: `422 validation_error` antes de consultar cualquier fuente."""
        cases = [
            ({"from": "ayer", "to": "2026-11-02T00:00:00Z"}, "query.from", "invalid_datetime"),
            (
                {"from": "2026-11-02T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
                "query.to",
                "ends_before_starts",
            ),
            (
                {"from": "2026-11-01T00:00:00Z", "to": "2027-02-01T00:00:01Z"},
                "query.to",
                "out_of_range",
            ),
            ({"from": "2026-11-01T00:00:00Z"}, "query.to", "required"),
        ]
        for params, field, issue in cases:
            with self.subTest(issue=issue):
                response = self.calendar("organizer", params)
                self.assertEqual(response.status_code, 422)
                body = response.json()
                self.assertEqual(body["code"], "validation_error")
                self.assertEqual(body["details"], [{"field": field, "issue": issue}])
        self.assertEqual(self.domains.requests, [])

    def test_default_period_is_the_current_month(self) -> None:
        body = self.assert_ok(self.calendar("organizer", {}))
        self.assertTrue(body["from"].endswith("-01T00:00:00Z"))
        self.assertTrue(body["to"].endswith("T23:59:59Z"))

    # ------------------------------------------------------------- parcial y caída total

    def test_partial_when_one_source_fails(self) -> None:
        """CA5: una fuente caída, lenta, con error o con una respuesta inválida se declara y
        las demás se entregan."""
        self.domains.data[EVENTS] = [event(at(8))]
        self.domains.data[SESSIONS] = [session(date(2026, 11, 9))]
        failures: list[tuple[Callable[[], None], str]] = [
            (lambda: self.domains.failure.__setitem__(SESSIONS, httpx.ConnectError), "UNAVAILABLE"),
            (lambda: self.domains.failure.__setitem__(SESSIONS, httpx.ReadTimeout), "TIMEOUT"),
            (lambda: self.domains.status.__setitem__(SESSIONS, (500, error("x"))), "UNAVAILABLE"),
            (
                lambda: self.domains.payload.__setitem__(
                    SESSIONS, {"items": [{}], "next_cursor": None}
                ),
                "UNAVAILABLE",
            ),
        ]
        for fail, status in failures:
            with self.subTest(status=status):
                self.domains.failure.clear()
                self.domains.status.clear()
                self.domains.payload.clear()
                fail()
                body = self.assert_ok(self.calendar("athlete"))
                self.assertIn(("TRAINING_PLAN_SESSION", status, False), self.sources(body))
                self.assertIn(("EVENT_PUBLISHED", "OK", False), self.sources(body))
                self.assertEqual([i["source"] for i in body["items"]], ["EVENT_PUBLISHED"])

    def test_all_sources_down_is_503_or_504(self) -> None:
        """CA5: con todas las fuentes caídas no se entrega un calendario vacío."""
        all_paths = (
            EVENTS,
            SESSIONS,
            ASSOCIATIONS,
            REGISTRATIONS,
            MY_EVENTS,
            MY_SERVICES,
            APPOINTMENTS,
        )
        for failure, status, code in (
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.ConnectError, 503, "upstream_unavailable"),
        ):
            for actor in ("athlete", "organizer", "service_provider"):
                with self.subTest(actor=actor, failure=failure):
                    self.domains.failure = dict.fromkeys(all_paths, failure)
                    response = self.calendar(actor)
                    self.assertEqual(
                        (response.status_code, response.json()["code"]), (status, code)
                    )
        self.domains.failure = {
            EVENTS: httpx.ReadTimeout,
            SESSIONS: httpx.ConnectError,
            ASSOCIATIONS: httpx.ConnectError,
            REGISTRATIONS: httpx.ConnectError,
        }
        mixed = self.calendar("athlete")
        self.assertEqual((mixed.status_code, mixed.json()["code"]), (503, "upstream_unavailable"))

    # ------------------------------------------------------------------ límite por fuente

    def test_truncated_single_query_sources(self) -> None:
        """CA6: más de 200 eventos publicados → los 200 primeros y `truncated`."""
        self.domains.data[EVENTS] = [event(at(1) + timedelta(hours=n)) for n in range(201)]
        body = self.assert_ok(self.calendar("athlete"))
        self.assertIn(("EVENT_PUBLISHED", "OK", True), self.sources(body))
        self.assertEqual(len(body["items"]), 200)

    def test_truncated_paged_sources(self) -> None:
        """CA6: eventos propios y servicios propios con más de 200 elementos se recortan a
        los primeros 200; los eventos dejan de leerse al pasar el límite."""
        self.domains.data[MY_EVENTS] = [event(at(1) + timedelta(hours=n)) for n in range(450)]
        organizer = self.assert_ok(self.calendar("organizer"))
        self.assertEqual(self.sources(organizer), [("OWN_EVENT", "OK", True)])
        self.assertEqual(len(organizer["items"]), 200)
        self.assertEqual(len(self.domains.queries(MY_EVENTS)), 3)

        self.domains.data[MY_SERVICES] = [
            service(*(at(2 + n % 20, 6 + h) for h in range(11))) for n in range(20)
        ]
        provider = self.assert_ok(self.calendar("service_provider"))
        self.assertIn(("OWN_SERVICE_AVAILABILITY", "OK", True), self.sources(provider))
        own = [i for i in provider["items"] if i["source"] == "OWN_SERVICE_AVAILABILITY"]
        self.assertEqual(len(own), 200)
        self.assertEqual(
            own, sorted(own, key=lambda i: (i["occurs_on"], i["starts_at"], i["item_id"]))
        )


class SourceDeadlineTest(unittest.TestCase):
    """Una fuente con varias páginas respeta su propio plazo y se declara `TIMEOUT`."""

    def test_paged_source_stops_at_its_deadline(self) -> None:
        domains = Domains()
        domains.data[MY_EVENTS] = [event(at(1) + timedelta(hours=n)) for n in range(150)]
        ticks = iter(range(100))
        identity = VerifiedAccessToken(
            subject=str(uuid.uuid4()),
            actor_type="organizer",
            token_id="t",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        http = httpx.Client(transport=httpx.MockTransport(domains), base_url="http://domains")
        app = FastAPI()
        app.include_router(
            build_integrated_calendar_router(
                verify_token=lambda _: identity,
                permissions=load_actor_permissions(resolve_actor_permissions_path()),
                athlete_snapshot=lambda _: {},
                events=EventsManagementClient(http),
                services=ComplementaryServicesClient(http),
                training=TrainingManagementClient(http),
                channel="web",
                source_timeout=0.5,
                monotonic=lambda: float(next(ticks)),
            )
        )

        response = TestClient(app).get(PATH, params=PERIOD, headers={"Authorization": "Bearer x"})

        self.assertEqual((response.status_code, response.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(len(domains.queries(MY_EVENTS)), 1)


if __name__ == "__main__":
    unittest.main()
