"""HU039 (ADR-045 §8) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): el contrato
interno `POST /v1/users/athletes/search` lee `user_directory` y las secciones `demographic` y
`sport_habits` de `athlete_profile_view`.

La mayoría de las pruebas siembran directamente la vista con la forma que deja el proyector
(muchas filas sin recorrer eventos); `test_projected_profile_is_found` recorre el proyector
real. Se comprueban la coincidencia por deporte, ciudad (sin mayúsculas, acentos ni espacios)
y país, las exclusiones y la paginación keyset completa por lotes.
"""

import json
import unittest
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from projection_support import ReadDatabase, profile_envelope, read_database, registered_envelope
from sqlalchemy import create_engine, insert, pool
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.athletes_search import build_athletes_search_router
from users_management.application.errors import ReadModelUnavailable
from users_management.application.messages import parse_users_event
from users_management.application.ports import AthleteSearchCriteria
from users_management.application.queries.athletes_search import SearchAthletes
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.terms import normalize_term
from users_management.infrastructure.persistence_read.athletes_search import (
    SqlAlchemyAthleteSearchReader,
)
from users_management.infrastructure.persistence_read.models import (
    AthleteProfileViewRow,
    UserDirectoryRow,
)


def _criteria(
    sports: Iterable[str] = ("running",),
    city: str = "Bogotá",
    country_code: str = "CO",
    limit: int = 1000,
    after: UUID | None = None,
) -> AthleteSearchCriteria:
    return AthleteSearchCriteria(
        sports=tuple(sports),
        city=normalize_term(city),
        country_code=country_code,
        limit=limit,
        after=after,
    )


def _sport_habits(sports: Iterable[str]) -> dict[str, Any]:
    """Documento de la sección como lo proyecta HU006 (sin `user_id`)."""
    return {
        "version": 1,
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
        "sports": [
            {
                "sport": sport,
                "frequency_per_week": 3,
                "session_duration_minutes": 45,
                "intensity": "MODERATE",
                "level": "INTERMEDIATE",
                "years_practicing": None,
            }
            for sport in sports
        ],
        "availability": {"days": ["MON"], "time_slots": ["MORNING"]},
        "primary_goal": None,
    }


def _demographic(city: str, country_code: str) -> dict[str, Any]:
    """Documento de la sección como lo proyecta HU005 (sin `user_id`)."""
    return {
        "version": 1,
        "created_at": "2026-10-16T12:00:00Z",
        "updated_at": "2026-10-16T12:00:00Z",
        "birth_date": "1990-05-14",
        "gender": "FEMALE",
        "country_code": country_code,
        "city": city,
        "marital_status": None,
        "occupation": None,
        "income_range": "PREFER_NOT_TO_SAY",
    }


class AthletesSearchPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read: ReadDatabase = self.enterContext(read_database("users_management_search"))
        self.reader = SqlAlchemyAthleteSearchReader(self.read.sessions)

    def seed(
        self,
        count: int = 1,
        *,
        sports: Iterable[str] | None = ("running",),
        city: str | None = "Bogotá",
        country_code: str = "CO",
        actor_type: str = "athlete",
        status: str = "active",
        with_profile: bool = True,
    ) -> list[UUID]:
        """Siembra `count` cuentas con la misma forma; devuelve sus identificadores."""
        now = datetime.now(UTC)
        user_ids = [uuid4() for _ in range(count)]
        sport_list = list(sports) if sports is not None else None
        directory = [
            {
                "user_id": user_id,
                "full_name": "Deportista Sintético",
                "email": f"atleta-{user_id.hex}@example.com",
                "actor_type": actor_type,
                "status": status,
                "aggregate_version": 1,
                "projected_at": now,
            }
            for user_id in user_ids
        ]
        profiles = [
            {
                "user_id": user_id,
                "demographic": None if city is None else _demographic(city, country_code),
                "demographic_version": None if city is None else 1,
                "sport_habits": None if sport_list is None else _sport_habits(sport_list),
                "sport_habits_version": None if sport_list is None else 1,
                "projected_at": now,
            }
            for user_id in user_ids
        ]
        with self.read.sessions() as session, session.begin():
            session.execute(insert(UserDirectoryRow), directory)
            if with_profile:
                session.execute(insert(AthleteProfileViewRow), profiles)
        return user_ids

    def found(self, criteria: AthleteSearchCriteria) -> list[UUID]:
        return list(self.reader.search(criteria).user_ids)

    def test_projected_profile_is_found(self) -> None:
        """Camino real: registro, HU005 y HU006 proyectados por `usuarios-read-projector`."""
        project = self.read.projector()
        user_id = uuid4()
        for envelope in (
            registered_envelope(user_id),
            profile_envelope(ProfileSection.DEMOGRAPHIC, user_id, 1),
            profile_envelope(ProfileSection.SPORT_HABITS, user_id, 1),
        ):
            project(parse_users_event(json.loads(json.dumps(envelope))))

        # La sección demográfica sintética tiene `city = "Ciudad 1"` y `country_code = "CO"`.
        self.assertEqual(self.found(_criteria(city="  CIUDAD 1 ")), [user_id])
        self.assertEqual(self.found(_criteria(sports=("cycling",), city="Ciudad 1")), [])

    def test_city_matches_without_case_accents_or_outer_spaces(self) -> None:
        stored = {
            "Bogotá": self.seed(city="Bogotá")[0],
            "BOGOTA": self.seed(city="BOGOTA")[0],
            "  bogotá  ": self.seed(city="  bogotá  ")[0],
        }
        other_city = self.seed(city="Bogotá D.C.")[0]
        expected = sorted(stored.values())

        for searched in ("Bogotá", "bogota", "BOGOTÁ", " Bogota "):
            with self.subTest(searched=searched):
                found = self.found(_criteria(city=searched))

                self.assertEqual(found, expected)
                self.assertNotIn(other_city, found)

    def test_accents_beyond_spanish_are_normalized_the_same_in_sql(self) -> None:
        """La tabla de `normalize_term` y la de `translate` en SQL son la misma."""
        for city in ("São Paulo", "Zürich", "Cañón del Chicamocha", "Kraków"):
            with self.subTest(city=city):
                user_id = self.seed(city=city)[0]

                found = self.found(_criteria(city=normalize_term(city).upper()))

                self.assertIn(user_id, found)

    def test_country_must_be_equal(self) -> None:
        colombian = self.seed(country_code="CO")[0]
        self.seed(country_code="MX")

        self.assertEqual(self.found(_criteria(country_code="CO")), [colombian])
        self.assertEqual(len(self.found(_criteria(country_code="MX"))), 1)
        self.assertEqual(self.found(_criteria(country_code="AR")), [])

    def test_any_requested_sport_is_enough(self) -> None:
        runner = self.seed(sports=("running",))[0]
        cyclist = self.seed(sports=("cycling",))[0]
        triathlete = self.seed(sports=("running", "cycling", "swimming"))[0]
        self.seed(sports=("other",))
        self.seed(sports=("strength",))

        self.assertEqual(self.found(_criteria(sports=("running",))), sorted([runner, triathlete]))
        self.assertEqual(
            self.found(_criteria(sports=("running", "cycling"))),
            sorted([runner, cyclist, triathlete]),
        )
        self.assertEqual(self.found(_criteria(sports=("swimming",))), [triathlete])

    def test_only_active_athletes_with_both_sections_match(self) -> None:
        athlete = self.seed()[0]
        self.seed(status="disabled")
        self.seed(actor_type="organizer")
        self.seed(actor_type="service_provider")
        self.seed(sports=None)  # Solo perfil demográfico (sin HU006).
        self.seed(city=None)  # Solo hábitos deportivos (sin HU005).
        self.seed(with_profile=False)  # Cuenta sin perfil.

        page = self.reader.search(_criteria())

        self.assertEqual(page.user_ids, (athlete,))
        self.assertFalse(page.has_more)

    def test_pages_cover_every_match_once_in_order(self) -> None:
        """25 coincidencias con `limit` 10: tres páginas sin duplicados ni huecos."""
        expected = sorted(self.seed(25))
        self.seed(5, city="Medellín")  # Ruido que no coincide.

        pages: list[tuple[UUID, ...]] = []
        after: UUID | None = None
        while True:
            page = self.reader.search(_criteria(limit=10, after=after))
            pages.append(page.user_ids)
            if not page.has_more:
                break
            after = page.user_ids[-1]

        self.assertEqual([len(item) for item in pages], [10, 10, 5])
        flattened = [user_id for item in pages for user_id in item]
        self.assertEqual(flattened, expected)
        self.assertEqual(len(set(flattened)), 25)

    def test_exact_multiple_of_the_limit_ends_with_an_empty_flag(self) -> None:
        expected = sorted(self.seed(20))

        first = self.reader.search(_criteria(limit=10))
        second = self.reader.search(_criteria(limit=10, after=first.user_ids[-1]))

        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        self.assertEqual(list(first.user_ids + second.user_ids), expected)

    def test_one_thousand_matches_fit_in_one_page(self) -> None:
        """HA017: un evento con 1.000 destinatarios coincidentes se resuelve en una página."""
        expected = sorted(self.seed(1000))

        page = self.reader.search(_criteria(limit=1000))

        self.assertEqual(list(page.user_ids), expected)
        self.assertFalse(page.has_more)

        extra = self.seed()[0]
        first = self.reader.search(_criteria(limit=1000))
        rest = self.reader.search(_criteria(limit=1000, after=first.user_ids[-1]))

        self.assertTrue(first.has_more)
        self.assertEqual(len(first.user_ids) + len(rest.user_ids), 1001)
        self.assertFalse(rest.has_more)
        self.assertIn(extra, first.user_ids + rest.user_ids)

    def test_http_contract_walks_every_page_with_the_cursor(self) -> None:
        """Por HTTP, como el worker de Eventos: recorre las páginas con el `next_cursor` opaco."""
        expected = sorted(str(user_id) for user_id in self.seed(25))
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_athletes_search_router(SearchAthletes(self.reader))],
        )
        client = TestClient(app)

        collected: list[str] = []
        cursor: str | None = None
        requests = 0
        while True:
            response = client.post(
                "/v1/users/athletes/search",
                json={
                    "sports": ["running", "cycling"],
                    "city": "BOGOTA",
                    "country_code": "CO",
                    "limit": 10,
                    "cursor": cursor,
                },
            )
            requests += 1
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            collected.extend(response.json()["user_ids"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break

        self.assertEqual(requests, 3)
        self.assertEqual(collected, expected)

    def test_unreachable_read_database_raises_read_model_unavailable(self) -> None:
        engine = create_engine(
            "postgresql+psycopg://nadie@127.0.0.1:1/ninguna",
            poolclass=pool.NullPool,
            connect_args={"connect_timeout": 2},
        )
        self.addCleanup(engine.dispose)
        reader = SqlAlchemyAthleteSearchReader(sessionmaker(engine))

        with self.assertRaises(ReadModelUnavailable) as raised:
            reader.search(_criteria())

        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(raised.exception.code, "upstream_unavailable")


if __name__ == "__main__":
    unittest.main()
