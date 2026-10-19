"""HU020: disponibilidad de los servicios publicados para el deportista (ADR-038)."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import test_create_service as hu015
from fakes import NOW, FixedClock
from fastapi.testclient import TestClient

from complementary_services.api.availability import build_availability_router
from complementary_services.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    PermissionRequired,
)
from complementary_services.application.ports import AvailabilityQuery, Caller
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.application.queries.service_availability import (
    MEDICAL_CAPABILITY,
    AvailabilityFilters,
    ListServiceAvailability,
    decode_cursor,
)
from complementary_services.domain.availability.intervals import AvailabilityInterval
from complementary_services.domain.catalog.offer import (
    BillingUnit,
    Category,
    Currency,
    Modality,
    Price,
    ServiceOffer,
    ServiceStatus,
)
from complementary_services.domain.validation import Issue
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

FROM = "2026-11-01T00:00:00-05:00"
TO = "2026-11-30T23:59:59-05:00"
START = datetime(2026, 11, 1, 5, tzinfo=UTC)


def interval(starts_at: datetime) -> AvailabilityInterval:
    return AvailabilityInterval(
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        time_zone="America/Bogota",
        capacity=3,
    )


def offer(
    name: str,
    *,
    category: Category = Category.COACHING,
    modality: Modality = Modality.VIRTUAL,
    status: ServiceStatus = ServiceStatus.PUBLISHED,
    starts: tuple[datetime, ...] = (START + timedelta(days=2),),
) -> ServiceOffer:
    return ServiceOffer(
        service_id=uuid4(),
        provider_id=uuid4(),
        name=name,
        description="Servicio sintético para pruebas de disponibilidad.",
        category=category,
        modality=modality,
        price=Price(Decimal("120000.00"), Currency.COP, BillingUnit.SESSION),
        availability=tuple(interval(item) for item in starts),
        status=status,
        version=2 if status is ServiceStatus.PUBLISHED else 1,
        created_at=NOW,
        updated_at=NOW,
        published_at=NOW if status is ServiceStatus.PUBLISHED else None,
    )


class InMemoryCatalogReader:
    """Aplica la consulta como lo hace la base: publicados, filtros, rango y keyset."""

    def __init__(self, offers: list[ServiceOffer]) -> None:
        self.offers = offers
        self.queries: list[AvailabilityQuery] = []

    def published_availability(self, query: AvailabilityQuery) -> list[ServiceOffer]:
        self.queries.append(query)
        result: list[ServiceOffer] = []
        for item in sorted(self.offers, key=lambda o: (o.name, str(o.service_id))):
            in_range = tuple(
                i
                for i in item.availability
                if query.starts_from <= i.starts_at < query.starts_before
                and i.starts_at > query.now
            )
            if item.status is not ServiceStatus.PUBLISHED:
                continue
            if query.categories is not None and item.category not in query.categories:
                continue
            if query.modalities is not None and item.modality not in query.modalities:
                continue
            if query.only_available and not in_range:
                continue
            if query.after is not None and (item.name, str(item.service_id)) <= (
                query.after.name,
                str(query.after.service_id),
            ):
                continue
            result.append(replace(item, availability=in_range))
        return result[: query.limit]


class FakeCapabilities:
    def __init__(self, granted: frozenset[str] = frozenset()) -> None:
        self.granted = granted
        self.failure: type[CapabilitiesUnavailable] | None = None
        self.calls = 0

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        self.calls += 1
        if self.failure is not None:
            raise self.failure()
        return capability in self.granted


class AvailabilityQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.coaching = offer("Entrenamiento funcional")
        self.medical = offer("Valoración médica deportiva", category=Category.SPORTS_MEDICINE)
        self.empty = offer("Nutrición deportiva", category=Category.NUTRITION, starts=())
        self.draft = offer("Borrador oculto", status=ServiceStatus.DRAFT)
        self.reader = InMemoryCatalogReader([self.coaching, self.medical, self.empty, self.draft])
        self.capabilities = FakeCapabilities()
        self.athlete = Caller(uuid4(), "athlete")

    def query(self, **overrides: Any) -> Any:
        filters = AvailabilityFilters(**{"starts_from": FROM, "starts_before": TO, **overrides})
        return ListServiceAvailability(
            reader=self.reader, capabilities=self.capabilities, clock=FixedClock()
        )(self.athlete, filters)

    def test_without_capability_medical_is_restricted(self) -> None:
        """CA1: borradores excluidos; medicina deportiva solo con la capacidad."""
        page = self.query()

        self.assertEqual([item.name for item in page.items], ["Entrenamiento funcional"])
        self.assertEqual(page.restricted_categories, (Category.SPORTS_MEDICINE,))
        self.assertNotIn(Category.SPORTS_MEDICINE, self.reader.queries[-1].categories or ())

    def test_with_capability_medical_is_visible(self) -> None:
        self.capabilities.granted = frozenset({MEDICAL_CAPABILITY})

        page = self.query()

        self.assertEqual(
            [item.name for item in page.items],
            ["Entrenamiento funcional", "Valoración médica deportiva"],
        )
        self.assertEqual(page.restricted_categories, ())
        self.assertIsNone(self.reader.queries[-1].categories)

    def test_explicit_medical_filter_without_capability_is_empty_not_forbidden(self) -> None:
        page = self.query(categories=("SPORTS_MEDICINE",))

        self.assertEqual((page.items, page.next_cursor), ((), None))
        self.assertEqual(page.restricted_categories, (Category.SPORTS_MEDICINE,))
        self.assertEqual(self.reader.queries, [], "sin categorías permitidas no se lee la base")

    def test_non_medical_categories_do_not_ask_affiliation(self) -> None:
        self.capabilities.failure = CapabilitiesUnavailable

        page = self.query(categories=("COACHING", "NUTRITION"), availability="all")

        self.assertEqual(self.capabilities.calls, 0)
        self.assertEqual(page.restricted_categories, ())
        self.assertEqual(
            [(item.name, bool(item.availability)) for item in page.items],
            [("Entrenamiento funcional", True), ("Nutrición deportiva", False)],
        )

    def test_available_excludes_and_all_marks_services_without_intervals(self) -> None:
        """CA2."""
        self.assertNotIn("Nutrición deportiva", [i.name for i in self.query().items])
        self.assertIn("Nutrición deportiva", [i.name for i in self.query(availability="all").items])

    def test_affiliation_down_or_slow_propagates(self) -> None:
        for failure in (CapabilitiesUnavailable, CapabilitiesTimeout):
            self.capabilities.failure = failure
            with self.subTest(failure=failure), self.assertRaises(CapabilitiesUnavailable):
                self.query()

    def test_only_athletes(self) -> None:
        for actor in ("service_provider", "organizer"):
            self.athlete = Caller(uuid4(), actor)
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.query()

    def test_invalid_parameters(self) -> None:
        cases: list[tuple[dict[str, Any], str, Issue]] = [
            ({"starts_from": None}, "from", Issue.REQUIRED),
            ({"starts_before": None}, "to", Issue.REQUIRED),
            ({"starts_from": "2026-11-01"}, "from", Issue.UNKNOWN_VALUE),
            ({"starts_before": "mañana"}, "to", Issue.UNKNOWN_VALUE),
            ({"starts_before": FROM}, "to", Issue.ENDS_BEFORE_STARTS),
            ({"starts_before": "2026-12-02T00:00:01-05:00"}, "to", Issue.OUT_OF_RANGE),
            ({"categories": ("PRODUCTS",)}, "category", Issue.UNKNOWN_VALUE),
            ({"modalities": ("REMOTE",)}, "modality", Issue.UNKNOWN_VALUE),
            ({"availability": "some"}, "availability", Issue.UNKNOWN_VALUE),
            ({"limit": 0}, "limit", Issue.OUT_OF_RANGE),
            ({"limit": 51}, "limit", Issue.OUT_OF_RANGE),
            ({"cursor": "no-es-un-cursor"}, "cursor", Issue.UNKNOWN_VALUE),
        ]
        for overrides, field, issue in cases:
            filters = {"starts_from": FROM, "starts_before": TO, **overrides}
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(InvalidServicesQuery) as raised,
            ):
                ListServiceAvailability(
                    reader=self.reader, capabilities=self.capabilities, clock=FixedClock()
                )(self.athlete, AvailabilityFilters(**filters))
            self.assertEqual((raised.exception.field, raised.exception.issue), (field, issue))
        self.assertEqual(self.capabilities.calls, 0, "un parámetro inválido no consulta Afiliación")

    def test_exactly_31_days_is_valid(self) -> None:
        self.assertEqual(len(self.query(starts_before="2026-12-02T00:00:00-05:00").items), 1)

    def test_keyset_pagination_is_stable(self) -> None:
        """CA4: páginas sin pérdidas ni repeticiones, en orden por nombre."""
        self.reader.offers = [offer(f"Servicio {index:02d}") for index in range(7)]
        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = self.query(limit=3, cursor=cursor)
            seen.extend(item.name for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
            self.assertEqual(decode_cursor(cursor).name, page.items[-1].name)

        self.assertEqual(seen, [f"Servicio {index:02d}" for index in range(7)])


class AvailabilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = InMemoryCatalogReader(
            [
                offer("Entrenamiento funcional", starts=(START + timedelta(days=1),)),
                offer("Valoración médica", category=Category.SPORTS_MEDICINE),
            ]
        )
        self.capabilities = FakeCapabilities()
        app = create_service_app(
            service_name="complementary-services",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_availability_router(
                    verify_token=hu015.VERIFIER.verify,
                    list_availability=ListServiceAvailability(
                        reader=self.reader, capabilities=self.capabilities, clock=FixedClock()
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, query: str, actor: str | None = "athlete") -> Any:
        return self.client.get(
            f"/v1/services/availability?{query}",
            headers={"Authorization": f"Bearer {hu015.token(uuid4(), actor)}"},
        )

    def test_contract(self) -> None:
        """CA1, CA6: intervalos en UTC `Z` con su zona y tarifa como cadena informativa."""
        response = self.get(f"from={FROM.replace('+', '%2B')}&to={TO}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(body["restricted_categories"], ["SPORTS_MEDICINE"])
        self.assertIsNone(body["next_cursor"])
        [item] = body["items"]
        self.assertEqual(
            item["price"], {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"}
        )
        self.assertEqual(
            item["availability"],
            [
                {
                    "starts_at": "2026-11-02T05:00:00Z",
                    "ends_at": "2026-11-02T06:00:00Z",
                    "time_zone": "America/Bogota",
                    "capacity": 3,
                }
            ],
        )
        self.assertTrue(item["available"])
        self.assertNotIn("status", item)

    def test_repeated_filters(self) -> None:
        self.capabilities.granted = frozenset({MEDICAL_CAPABILITY})

        response = self.get(
            f"from={FROM}&to={TO}&category=SPORTS_MEDICINE&category=COACHING&modality=VIRTUAL"
        )

        self.assertEqual(len(response.json()["items"]), 2)
        query = self.reader.queries[-1]
        self.assertEqual(query.categories, {Category.SPORTS_MEDICINE, Category.COACHING})
        self.assertEqual(query.modalities, {Modality.VIRTUAL})

    def test_errors(self) -> None:
        organizer = self.get(f"from={FROM}&to={TO}", actor="organizer")
        missing = self.get(f"to={TO}")
        bad_limit = self.get(f"from={FROM}&to={TO}&limit=diez")
        self.capabilities.failure = CapabilitiesTimeout
        affiliation_down = self.get(f"from={FROM}&to={TO}")
        anonymous = self.client.get(f"/v1/services/availability?from={FROM}&to={TO}")

        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (missing.status_code, missing.json()["details"]),
            (422, [{"field": "query.from", "issue": "required"}]),
        )
        self.assertEqual(
            bad_limit.json()["details"], [{"field": "query.limit", "issue": "unknown_value"}]
        )
        self.assertEqual(
            (affiliation_down.status_code, affiliation_down.json()["code"]),
            (503, "upstream_unavailable"),
        )
        self.assertEqual(anonymous.status_code, 401)


if __name__ == "__main__":
    unittest.main()
