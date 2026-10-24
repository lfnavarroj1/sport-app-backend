"""HU026 (ADR-041): reglas del agregado `Route`, retiro, documento de estado completo y reglas
deterministas de sugerencia, sin infraestructura."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from route_fakes import route_input

from training_management.application.route_documents import (
    InvalidRouteDocument,
    route_from_document,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routes.events import route_document
from training_management.domain.routes.model import (
    GeoPoint,
    InvalidRoute,
    Route,
    RouteStatus,
    distance_in_range,
    format_distance,
)
from training_management.domain.routes.suggestions import (
    NON_PERSONALIZED_MAX,
    SuggestionContext,
    SuggestionReason,
    city_key,
    personalized_order,
    reasons_for,
    suggestion_limit,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def publish(**overrides: Any) -> Route:
    return Route.publish(
        route_id=uuid4(), organizer_id=uuid4(), data=route_input(**overrides), now=NOW
    )


def issues(**overrides: Any) -> list[tuple[str, str]]:
    with unittest.TestCase().assertRaises(InvalidRoute) as error:
        publish(**overrides)
    return [(item.field, item.issue) for item in error.exception.violations]


class RouteModelTest(unittest.TestCase):
    def test_valid_route_is_published_in_version_1_normalized(self) -> None:
        """CA1: nace `PUBLISHED`, versión 1, con distancia de un decimal y textos recortados."""
        route = publish(
            name="  Vuelta   al  embalse ",
            description="   ",
            distance_km=Decimal("12"),
            city=" Santa  Marta ",
        )

        self.assertEqual((route.status, route.version), (RouteStatus.PUBLISHED, 1))
        self.assertEqual(route.name, "Vuelta al embalse")
        self.assertIsNone(route.description)
        self.assertEqual(route.city, "Santa Marta")
        self.assertEqual(format_distance(route.distance_km), "12.0")
        self.assertEqual((route.created_at, route.published_at, route.retired_at), (NOW, NOW, None))

    def test_distance_must_have_one_decimal_between_0_1_and_500(self) -> None:
        """CA2: distancia fuera de rango o con más de un decimal."""
        for valid in ("0.1", "500.0", "500", "42.2"):
            with self.subTest(valid=valid):
                self.assertTrue(distance_in_range(Decimal(valid)))
        for invalid in ("0.0", "0.09", "500.1", "1.25", "-1", "NaN", "Infinity"):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    issues(distance_km=Decimal(invalid)), [("distance_km", "out_of_range")]
                )

    def test_coordinates_must_be_in_range(self) -> None:
        """CA2: coordenadas inválidas en el inicio o en un punto intermedio."""
        self.assertEqual(
            issues(
                start_point=GeoPoint(lat=90.5, lng=-180.01),
                waypoints=(GeoPoint(lat=1.0, lng=2.0), GeoPoint(lat=-91.0, lng=float("nan"))),
            ),
            [
                ("start_point.lat", "out_of_range"),
                ("start_point.lng", "out_of_range"),
                ("waypoints.1.lat", "out_of_range"),
                ("waypoints.1.lng", "out_of_range"),
            ],
        )
        self.assertEqual(publish(start_point=GeoPoint(lat=-90.0, lng=180.0)).version, 1)

    def test_texts_country_and_waypoints_limits(self) -> None:
        self.assertEqual(
            issues(name="ab", description="x" * 2001, city="x", country_code="XX"),
            [
                ("name", "invalid_length"),
                ("description", "invalid_length"),
                ("city", "invalid_length"),
                ("country_code", "unknown_value"),
            ],
        )
        point = GeoPoint(lat=1.0, lng=1.0)
        self.assertEqual(issues(waypoints=(point,) * 201), [("waypoints", "out_of_range")])
        self.assertEqual(len(publish(waypoints=(point,) * 200).waypoints), 200)

    def test_retire_bumps_the_version_once_and_keeps_the_route(self) -> None:
        """CA4: `RETIRED` con la versión siguiente; solo una ruta publicada se retira."""
        route = publish()
        later = NOW + timedelta(days=1)

        retired = route.retire(later)

        self.assertEqual((retired.status, retired.version), (RouteStatus.RETIRED, 2))
        self.assertEqual((retired.retired_at, retired.updated_at), (later, later))
        self.assertEqual((retired.route_id, retired.name), (route.route_id, route.name))
        with self.assertRaises(ValueError):
            retired.retire(later)


class RouteDocumentTest(unittest.TestCase):
    def test_document_round_trips_and_uses_utc_z(self) -> None:
        route = publish().retire(NOW + timedelta(hours=1))
        document = route_document(route)

        self.assertEqual(route_from_document(document), route)
        self.assertEqual(document["published_at"], "2026-10-24T12:00:00Z")
        self.assertEqual(document["retired_at"], "2026-10-24T13:00:00Z")
        self.assertEqual(document["source"], "ORGANIZER")
        self.assertEqual(document["distance_km"], "12.5")

    def test_invalid_documents_are_rejected_with_a_reason(self) -> None:
        document = route_document(publish())
        cases = {
            "distance_km_invalid": {"distance_km": "12.55"},
            "country_code_invalid": {"country_code": "ZZ"},
            "start_point_out_of_range": {"start_point": {"lat": 100, "lng": 0}},
            "start_point_invalid": {"start_point": {"lat": "1", "lng": 0}},
            "status_invalid": {"status": "DRAFT"},
            "retired_at_mismatch": {"status": "RETIRED"},
            "version_invalid": {"version": 0},
            "source_invalid": {"source": "ATHLETE"},
            "published_at_without_offset": {"published_at": "2026-10-24T12:00:00"},
            "route_id_not_uuid": {"route_id": "x"},
            "waypoints_invalid": {"waypoints": [{"lat": 0, "lng": 0}] * 201},
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidRouteDocument) as error:
                route_from_document({**document, **change})
            self.assertEqual(error.exception.reason, reason)


class SuggestionRulesTest(unittest.TestCase):
    def test_city_key_ignores_case_accents_and_spaces(self) -> None:
        self.assertEqual(city_key("  Bogotá   D.C. "), city_key("BOGOTA d.c."))
        self.assertEqual(city_key("Medellín"), "medellin")

    def test_reasons_and_personalization(self) -> None:
        """CA3: `SPORT_MATCH` siempre al personalizar y `CITY_MATCH` con la misma ciudad y país."""
        context = SuggestionContext(sports=(Sport.RUNNING,), city="medellin", country_code="CO")

        self.assertEqual(
            reasons_for(context, city="Medellín", country_code="CO"),
            (SuggestionReason.SPORT_MATCH, SuggestionReason.CITY_MATCH),
        )
        self.assertEqual(
            reasons_for(context, city="Medellín", country_code="AR"),
            (SuggestionReason.SPORT_MATCH,),
        )
        without_country = SuggestionContext(sports=(Sport.RUNNING,), city="Medellín")
        self.assertIn(
            SuggestionReason.CITY_MATCH,
            reasons_for(without_country, city="MEDELLIN", country_code="AR"),
        )
        empty = SuggestionContext(sports=(), city="Medellín", country_code="CO")
        self.assertFalse(empty.personalized)
        self.assertEqual(reasons_for(empty, city="Medellín", country_code="CO"), ())

    def test_order_is_city_match_then_distance_then_route_id(self) -> None:
        """CA3: orden estable y determinista."""
        context = SuggestionContext(sports=(Sport.RUNNING,), city="Cali", country_code="CO")
        ids = sorted(uuid4() for _ in range(4))
        candidates = [
            ("Bogotá", Decimal("3.0"), ids[0]),
            ("Cali", Decimal("20.0"), ids[3]),
            ("Cali", Decimal("5.0"), ids[2]),
            ("Cali", Decimal("5.0"), ids[1]),
        ]

        ordered = sorted(
            candidates,
            key=lambda item: personalized_order(
                context, city=item[0], country_code="CO", distance_km=item[1], route_id=item[2]
            ),
        )

        self.assertEqual([item[2] for item in ordered], [ids[1], ids[2], ids[3], ids[0]])

    def test_non_personalized_list_is_capped_at_20(self) -> None:
        personalized = SuggestionContext(sports=(Sport.CYCLING,))
        empty = SuggestionContext(sports=())

        self.assertEqual(suggestion_limit(personalized, 50), 50)
        self.assertEqual(suggestion_limit(empty, 50), NON_PERSONALIZED_MAX)
        self.assertEqual(suggestion_limit(empty, 5), 5)

    def test_uuid_order_matches_postgres_byte_order(self) -> None:
        """El desempate por `route_id` de Python coincide con el de PostgreSQL (bytes)."""
        values = [uuid4() for _ in range(20)]
        self.assertEqual(sorted(values), sorted(values, key=lambda value: UUID(value.hex).bytes))


if __name__ == "__main__":
    unittest.main()
