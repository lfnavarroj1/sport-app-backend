"""Contrato interno `POST /v1/users/athletes/search` (ADR-045 §8, HU039) con un lector en
memoria: validación con `details[{field, issue}]`, ciudad normalizada, cursor opaco, `403` con
un token de cliente sin leer datos, `503` sin base de lectura y auditoría sin valores."""

import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.athletes_search import build_athletes_search_router
from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteSearchCriteria, AthleteSearchPage
from users_management.application.queries.athletes_search import (
    SEARCHABLE_SPORTS,
    SearchAthletes,
    decode_cursor,
    encode_cursor,
)
from users_management.domain.profile.terms import normalize_term

AUDIT = "audit.athletes_search"
PATH = "/v1/users/athletes/search"
VALID = {"sports": ["running"], "city": "Bogotá", "country_code": "CO"}


class InMemoryAthletes:
    """Devuelve siempre `page` y registra los criterios recibidos."""

    def __init__(self) -> None:
        self.page = AthleteSearchPage(user_ids=(), has_more=False)
        self.unavailable = False
        self.queries: list[AthleteSearchCriteria] = []

    def search(self, criteria: AthleteSearchCriteria) -> AthleteSearchPage:
        self.queries.append(criteria)
        if self.unavailable:
            raise ReadModelUnavailable()
        return self.page


class NormalizeTermTest(unittest.TestCase):
    def test_trims_lowercases_and_removes_accents(self) -> None:
        for raw, expected in (
            ("  Bogotá ", "bogota"),
            ("MEDELLÍN", "medellin"),
            ("São Paulo", "sao paulo"),
            ("Cañón", "canon"),
            ("Zürich", "zurich"),
            ("bogota", "bogota"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_term(raw), expected)


class CursorTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        user_id = uuid.uuid4()

        self.assertEqual(decode_cursor(encode_cursor(user_id)), user_id)

    def test_foreign_cursors_are_rejected(self) -> None:
        for cursor in ("", "no-es-base64!", "e30", "eyJ1IjoieCJ9", "a" * 600):
            with self.subTest(cursor=cursor):
                self.assertIsNone(decode_cursor(cursor))


class AthletesSearchApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = InMemoryAthletes()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_athletes_search_router(SearchAthletes(self.reader))],
        )
        self.client = TestClient(app)

    def search(self, body: object, **headers: str) -> object:
        return self.client.post(PATH, json=body, headers=headers)

    def audited(self, audit: object) -> list[dict[str, object]]:
        return [
            record.fields  # type: ignore[attr-defined]
            for record in audit.records  # type: ignore[attr-defined]
            if record.getMessage() == AUDIT
        ]

    def details(self, body: object) -> list[dict[str, object]]:
        response = self.search(body)
        self.assertEqual(response.status_code, 422, response.text)  # type: ignore[attr-defined]
        self.assertEqual(response.json()["code"], "validation_error")  # type: ignore[attr-defined]
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        details: list[dict[str, object]] = response.json()["details"]  # type: ignore[attr-defined]
        return details

    def assert_invalid(self, body: object, field: str | None, issue: str) -> None:
        self.assertIn({"field": field, "issue": issue}, self.details(body))
        self.assertEqual(self.reader.queries, [])

    def test_matching_page_returns_only_identifiers(self) -> None:
        """CA1 (HU039): identificadores en orden; criterios normalizados para la base."""
        first, second = sorted((uuid.uuid4(), uuid.uuid4()))
        self.reader.page = AthleteSearchPage(user_ids=(first, second), has_more=False)
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.search(
                {
                    "sports": ["swimming", "running"],
                    "city": "  Bogotá ",
                    "country_code": "CO",
                    "limit": 2,
                    "cursor": None,
                }
            )

        self.assertEqual(response.status_code, 200, response.text)  # type: ignore[attr-defined]
        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {"user_ids": [str(first), str(second)], "next_cursor": None},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual(
            self.reader.queries,
            [
                AthleteSearchCriteria(
                    sports=("running", "swimming"),
                    city="bogota",
                    country_code="CO",
                    limit=2,
                    after=None,
                )
            ],
        )
        self.assertEqual(
            self.audited(audit),
            [{"consumer": "events-management", "result": "found", "count": 2}],
        )

    def test_defaults_to_a_page_of_one_thousand(self) -> None:
        response = self.search(VALID)

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual([criteria.limit for criteria in self.reader.queries], [1000])
        self.assertIsNone(self.reader.queries[0].after)

    def test_more_rows_produce_a_cursor_to_the_last_identifier(self) -> None:
        user_ids = tuple(sorted(uuid.uuid4() for _ in range(3)))
        self.reader.page = AthleteSearchPage(user_ids=user_ids, has_more=True)

        response = self.search({**VALID, "limit": 3})

        next_cursor = response.json()["next_cursor"]  # type: ignore[attr-defined]
        self.assertIsInstance(next_cursor, str)
        self.assertEqual(decode_cursor(next_cursor), user_ids[-1])

        self.search({**VALID, "limit": 3, "cursor": next_cursor})

        self.assertEqual(self.reader.queries[-1].after, user_ids[-1])

    def test_empty_result_is_200(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.search(VALID)

        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {"user_ids": [], "next_cursor": None},
        )
        self.assertEqual([entry["count"] for entry in self.audited(audit)], [0])

    def test_all_searchable_sports_are_accepted(self) -> None:
        response = self.search({**VALID, "sports": list(reversed(SEARCHABLE_SPORTS))})

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual(self.reader.queries[0].sports, SEARCHABLE_SPORTS)

    def test_sports_rules(self) -> None:
        for body, field, issue in (
            ({"city": "Bogotá", "country_code": "CO"}, "body.sports", "required"),
            ({**VALID, "sports": None}, "body.sports", "required"),
            ({**VALID, "sports": []}, "body.sports", "required"),
            ({**VALID, "sports": "running"}, "body.sports", "unknown_value"),
            (
                {**VALID, "sports": ["running", "cycling", "swimming", "strength", "other"]},
                "body.sports",
                "too_many_items",
            ),
            ({**VALID, "sports": ["other"]}, "body.sports.0", "unknown_value"),
            ({**VALID, "sports": ["running", "yoga"]}, "body.sports.1", "unknown_value"),
            ({**VALID, "sports": ["RUNNING"]}, "body.sports.0", "unknown_value"),
            ({**VALID, "sports": [1]}, "body.sports.0", "unknown_value"),
            ({**VALID, "sports": ["running", "running"]}, "body.sports.1", "duplicate_value"),
        ):
            with self.subTest(body=body):
                self.assert_invalid(body, field, issue)

    def test_city_rules(self) -> None:
        for body, issue in (
            ({"sports": ["running"], "country_code": "CO"}, "required"),
            ({**VALID, "city": None}, "required"),
            ({**VALID, "city": 7}, "unknown_value"),
            ({**VALID, "city": "B"}, "invalid_length"),
            ({**VALID, "city": "  B  "}, "invalid_length"),
            ({**VALID, "city": "x" * 81}, "invalid_length"),
        ):
            with self.subTest(body=body):
                self.assert_invalid(body, "body.city", issue)

    def test_city_length_limits_are_inclusive(self) -> None:
        for city in ("Ab", "x" * 80):
            with self.subTest(city=city):
                response = self.search({**VALID, "city": city})

                self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]

    def test_country_rules(self) -> None:
        for body, issue in (
            ({"sports": ["running"], "city": "Bogotá"}, "required"),
            ({**VALID, "country_code": "co"}, "unknown_value"),
            ({**VALID, "country_code": "XX"}, "unknown_value"),
            ({**VALID, "country_code": "COL"}, "unknown_value"),
            ({**VALID, "country_code": 57}, "unknown_value"),
        ):
            with self.subTest(body=body):
                self.assert_invalid(body, "body.country_code", issue)

    def test_limit_rules(self) -> None:
        for limit, issue in (
            (0, "out_of_range"),
            (1001, "out_of_range"),
            (-1, "out_of_range"),
            ("10", "unknown_value"),
            (True, "unknown_value"),
            (2.5, "unknown_value"),
        ):
            with self.subTest(limit=limit):
                self.assert_invalid({**VALID, "limit": limit}, "body.limit", issue)

    def test_limit_bounds_are_inclusive(self) -> None:
        for limit in (1, 1000):
            with self.subTest(limit=limit):
                self.assertEqual(
                    self.search({**VALID, "limit": limit}).status_code,  # type: ignore[attr-defined]
                    200,
                )

    def test_invalid_cursor(self) -> None:
        for cursor, issue in (
            ("no-es-un-cursor", "invalid_cursor"),
            ("", "invalid_cursor"),
            ("a" * 600, "invalid_cursor"),
            (12, "unknown_value"),
        ):
            with self.subTest(cursor=cursor):
                self.assert_invalid({**VALID, "cursor": cursor}, "body.cursor", issue)

    def test_extra_fields_are_not_allowed(self) -> None:
        for name in ("user_id", "email", "Sports"):
            with self.subTest(name=name):
                self.assert_invalid({**VALID, name: "x"}, f"body.{name}", "field_not_allowed")

    def test_all_problems_are_reported_together(self) -> None:
        details = self.details({"sports": ["other"], "city": "B", "limit": 0, "extra": 1})

        self.assertEqual(
            details,
            [
                {"field": "body.extra", "issue": "field_not_allowed"},
                {"field": "body.sports.0", "issue": "unknown_value"},
                {"field": "body.city", "issue": "invalid_length"},
                {"field": "body.country_code", "issue": "required"},
                {"field": "body.limit", "issue": "out_of_range"},
            ],
        )

    def test_body_that_is_not_an_object(self) -> None:
        for raw, issue in (
            (b"", "required"),
            (b"no es json", "unknown_value"),
            (b"[]", "unknown_value"),
            (b"null", "required"),
        ):
            with self.subTest(raw=raw):
                response = self.client.post(
                    PATH, content=raw, headers={"Content-Type": "application/json"}
                )

                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"], [{"field": None, "issue": issue}])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.reader.queries, [])

    def test_validation_errors_never_echo_the_values(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.search({**VALID, "city": "Ciudad-Secreta" * 10, "sports": ["yoga"]})

        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]
        self.assertNotIn("Ciudad-Secreta", response.text)  # type: ignore[attr-defined]
        self.assertNotIn("yoga", response.text)  # type: ignore[attr-defined]
        self.assertEqual(
            self.audited(audit),
            [
                {
                    "consumer": "events-management",
                    "result": "rejected",
                    "count": 0,
                    "reason": "validation_error",
                }
            ],
        )

    def test_a_client_token_is_rejected_without_reading(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            # Ni siquiera un cuerpo inválido se evalúa: el token se rechaza primero.
            for body in (VALID, {"sports": "x"}):
                response = self.search(body, Authorization="Bearer token-de-cliente")

                self.assertEqual(
                    (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
                    (403, "permission_required"),
                )
                self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]

        self.assertEqual(self.reader.queries, [])
        self.assertEqual(
            self.audited(audit),
            [
                {
                    "consumer": "events-management",
                    "result": "rejected",
                    "count": 0,
                    "reason": "authorization_present",
                }
            ]
            * 2,
        )

    def test_read_model_down_is_503(self) -> None:
        self.reader.unavailable = True
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.search(VALID)

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (503, "upstream_unavailable"),
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual(
            self.audited(audit),
            [{"consumer": "events-management", "result": "unavailable", "count": 0}],
        )

    def test_audit_never_contains_city_sports_or_identifiers(self) -> None:
        user_id = uuid.uuid4()
        self.reader.page = AthleteSearchPage(user_ids=(user_id,), has_more=True)
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.search({**VALID, "sports": ["cycling"], "city": "Medellín"})

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        rendered = " ".join(audit.output) + str(self.audited(audit))
        for value in ("Medellín", "medellin", "cycling", str(user_id), "CO"):
            self.assertNotIn(value, rendered)

    def test_criteria_repr_hides_the_city(self) -> None:
        criteria = AthleteSearchCriteria(
            sports=("running",), city="medellin", country_code="CO", limit=10
        )

        self.assertNotIn("medellin", repr(criteria))
        self.assertNotIn("CO", repr(criteria))


if __name__ == "__main__":
    unittest.main()
