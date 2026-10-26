"""Componente HU023 en web: BFF → Eventos real (escritura, outbox, Pub/Sub, proyector y
lectura) → calendario del organizador y coincidencias de su borrador (ADR-040).

Mismo montaje que HU024 (`EventFlowCase`): PostgreSQL migrado (`TEST_DATABASE_URL`) y
emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
"""

import os
import unittest
import uuid

import httpx
from test_event_publication_flow import EventFlowCase

from events_management.workers.pubsub_setup import ensure_resources

PERIOD = {"from": "2099-11-14T00:00:00Z", "to": "2099-11-17T00:00:00Z"}


def location(city: str) -> dict[str, str]:
    return {
        "name": "Parque sintético",
        "address": "Calle 10 # 20-30",
        "city": city,
        "country_code": "CO",
    }


class EventCalendarFlowTest(EventFlowCase):
    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_calendar_and_conflicts_through_the_web_channel(self) -> None:
        """CA1-CA4: el borrador (07:00-10:00 -05:00, Running, Bogotá) se compara contra eventos
        publicados de otro organizador: uno coincide por ciudad, otro no comparte nada, otro
        solo toca el borde y un borrador ajeno nunca aparece."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        organizer, other = self.session_for("organizer"), self.session_for("organizer")
        draft = self.draft(organizer)
        by_city = self.draft(
            other,
            discipline="Ciclismo",
            location=location("BOGOTÁ"),
            starts_at="2099-11-15T08:00:00-05:00",
        )
        unrelated = self.draft(
            other,
            discipline="Ciclismo",
            location=location("Medellín"),
            starts_at="2099-11-15T08:00:00-05:00",
        )
        touching = self.draft(other, starts_at="2099-11-15T10:00:00-05:00")
        self.draft(other, starts_at="2099-11-15T08:00:00-05:00")  # borrador ajeno
        for event_id in (by_city, unrelated, touching):
            published = self.publish(other, event_id, str(uuid.uuid4()))
            self.assertEqual(published.status_code, 200, published.text)
        self.project_through_pubsub(project_id, processed=8)  # cinco creados y tres publicados

        # CA2: coincidencias del borrador propio.
        conflicts = self.bff.get(f"/v1/me/events/{draft}/conflicts", headers=organizer)
        self.assertEqual(conflicts.status_code, 200, conflicts.text)
        self.assertEqual(
            [(c["event_id"], c["reasons"]) for c in conflicts.json()["conflicts"]],
            [(by_city, ["SAME_CITY"])],
        )
        self.assertEqual(conflicts.json()["conflicts"][0]["city"], "BOGOTÁ")
        self.assertTrue(conflicts.json()["evaluated_at"].endswith("Z"))

        # CA1 y CA4: calendario con filtros normalizados, solo publicados y sin organizador.
        calendar = self.bff.get(
            "/v1/events", params={**PERIOD, "city": "bogota"}, headers=organizer
        )
        self.assertEqual(calendar.status_code, 200, calendar.text)
        self.assertEqual([i["event_id"] for i in calendar.json()["items"]], [by_city, touching])
        self.assertNotIn("organizer_id", calendar.text)
        self.assertEqual(calendar.headers["Cache-Control"], "no-store")
        cycling = self.bff.get(
            "/v1/events",
            params={**PERIOD, "discipline": "CICLISMO", "limit": "1"},
            headers=organizer,
        )
        second = self.bff.get(
            "/v1/events",
            params={**PERIOD, "discipline": "CICLISMO", "cursor": cycling.json()["next_cursor"]},
            headers=organizer,
        )
        self.assertEqual(
            [i["event_id"] for i in cycling.json()["items"] + second.json()["items"]],
            sorted([by_city, unrelated]),  # mismo inicio: desempata event_id
        )
        self.assertIsNone(second.json()["next_cursor"])

        # CA3 y permisos: periodo inválido, evento ajeno, otro actor.
        same = self.bff.get(
            "/v1/events", params={"from": PERIOD["from"], "to": PERIOD["from"]}, headers=organizer
        )
        self.assertEqual(
            same.json()["details"], [{"field": "query.to", "issue": "ends_before_starts"}]
        )
        foreign = self.bff.get(f"/v1/me/events/{by_city}/conflicts", headers=organizer)
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "event_not_found"))
        athlete = self.session_for("athlete")
        for path in ("/v1/events", f"/v1/me/events/{draft}/conflicts"):
            denied = self.bff.get(path, params=PERIOD, headers=athlete)
            self.assertEqual(
                (denied.status_code, denied.json()["code"]), (403, "permission_required")
            )

    def test_source_down_or_silent(self) -> None:
        """Eventos caído → `503 upstream_unavailable`; sin respuesta → `504 upstream_timeout`."""
        organizer = self.session_for("organizer")
        for failure, status, code in (
            (httpx.ConnectError, 503, "upstream_unavailable"),
            (httpx.ReadTimeout, 504, "upstream_timeout"),
        ):

            def down(request: httpx.Request, failure: type[Exception] = failure) -> httpx.Response:
                raise failure("eventos")

            bff = self.bff_with(
                httpx.Client(transport=httpx.MockTransport(down), base_url="http://events")
            )
            for path in ("/v1/events", f"/v1/me/events/{uuid.uuid4()}/conflicts"):
                with self.subTest(failure=failure.__name__, path=path):
                    response = bff.get(path, params=PERIOD, headers=organizer)
                    self.assertEqual(
                        (response.status_code, response.json()["code"]), (status, code)
                    )


if __name__ == "__main__":
    unittest.main()
