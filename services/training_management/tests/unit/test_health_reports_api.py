"""API de HU042 en el servicio: contrato de ADR-037, autenticación, autorización y errores."""

import logging
import unittest
from typing import Any
from uuid import uuid4

from fakes import FixedClock
from fastapi.testclient import TestClient
from health_fakes import (
    CONSENT,
    InMemoryHealthReportUnitOfWork,
    InMemoryHealthStore,
    health_body,
    training_session,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.health_reports import build_health_reports_router
from training_management.application.commands.report_health import ReportHealth

REPORTS = "/v1/me/health-reports"


class HealthReportsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryHealthStore()
        self.athlete_id = uuid4()
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_health_reports_router(
                    verify_token=VERIFIER.verify,
                    report_health=ReportHealth(
                        unit_of_work=lambda: InMemoryHealthReportUnitOfWork(self.store),
                        clock=FixedClock(),
                        consent_version=CONSENT,
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def post(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, "Idempotency-Key": key or str(uuid4()), **headers}
        return self.client.post(REPORTS, json=body, headers=sent)

    def test_created_report(self) -> None:
        """CA1: `201` `OPEN`, versión 1, del deportista del token."""
        body = health_body(session_id=None)

        response = self.post(body)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(
            response.json(),
            {
                "report_id": body["report_id"],
                "athlete_id": str(self.athlete_id),
                "type": "DISCOMFORT",
                "body_area": "KNEE",
                "severity": "MODERATE",
                "occurred_at": "2026-10-12T10:30:00Z",
                "session_id": None,
                "expected_recovery_days": 3,
                "consent": {"version": CONSENT},
                "status": "OPEN",
                "reported_at": "2026-10-12T12:00:00Z",
                "version": 1,
            },
        )

    def test_replay_and_existing_report(self) -> None:
        """CA3: misma clave → respuesta original; mismo `report_id` y datos → existente."""
        body, key = health_body(), str(uuid4())
        first = self.post(body, key)

        replayed = self.post(body, key)
        existing = self.post(body)

        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(replayed.json(), first.json())
        self.assertEqual(existing.status_code, 201)
        self.assertEqual(existing.json(), first.json())
        self.assertEqual(len(self.store.outbox), 1)

    def test_conflicts(self) -> None:
        body, key = health_body(), str(uuid4())
        self.post(body, key)

        reused_key = self.post(health_body(), key)
        reused_report = self.post({**body, "body_area": "BACK"})

        self.assertEqual(reused_key.status_code, 409)
        self.assertEqual(reused_key.json()["code"], "idempotency_key_reused")
        self.assertEqual(reused_report.status_code, 409)
        self.assertEqual(reused_report.json()["code"], "report_id_reused")

    def test_foreign_session_is_not_found(self) -> None:
        """CA2."""
        foreign = training_session(uuid4())
        self.store.sessions[foreign.session_id] = foreign

        response = self.post(health_body(session_id=str(foreign.session_id)))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "training_session_not_found")
        self.assertEqual(self.store.reports, {})

    def test_validation_error_with_details(self) -> None:
        """CA2: `422` con todas las reglas incumplidas; `athlete_id` en el cuerpo se rechaza."""
        body = health_body(athlete_id=str(uuid4()), severity="SEVERE", expected_recovery_days=999)
        del body["consent"]

        response = self.post(body, "no-es-uuid")

        self.assertEqual(response.status_code, 422)
        document = response.json()
        self.assertEqual(document["code"], "validation_error")
        self.assertEqual(
            {(item["field"], item["issue"]) for item in document["details"]},
            {
                ("Idempotency-Key", "invalid_uuid"),
                ("athlete_id", "field_not_allowed"),
                ("severity", "unknown_value"),
                ("expected_recovery_days", "out_of_range"),
                ("consent.version", "required"),
            },
        )
        self.assertEqual(self.store.reports, {})

    def test_empty_body_is_required(self) -> None:
        response = self.client.post(REPORTS, headers={**self.auth, "Idempotency-Key": str(uuid4())})

        self.assertEqual(response.status_code, 422)
        self.assertIn({"field": None, "issue": "required"}, response.json()["details"])

    def test_other_actors_are_forbidden(self) -> None:
        for actor_type in ("service_provider", "organizer"):
            with self.subTest(actor_type=actor_type):
                response = self.post(
                    health_body(), Authorization=f"Bearer {token(uuid4(), actor_type)}"
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.store.reports, {})

    def test_requires_a_valid_token(self) -> None:
        missing = self.client.post(REPORTS, json=health_body())
        invalid = self.post(health_body(), Authorization="Bearer x.y.z")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.json()["code"], "invalid_access_token")

    def test_logs_do_not_contain_health_data(self) -> None:
        body = health_body(type="INJURY", body_area="ANKLE_FOOT", severity="HIGH")

        with self.assertLogs(level=logging.DEBUG) as logs:
            self.post(body)
            self.post({**body, "severity": "LOW"})

        captured = " ".join(
            f"{record.getMessage()} {record.__dict__.get('fields', '')}" for record in logs.records
        )
        for value in ("INJURY", "ANKLE_FOOT", "HIGH", "LOW", "10:30:00", CONSENT):
            self.assertNotIn(value, captured)


if __name__ == "__main__":
    unittest.main()
