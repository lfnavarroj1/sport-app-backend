"""API de HU012 en el servicio: rutas del historial de salud con dobles en memoria."""

import logging
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fakes import NOW
from fastapi.testclient import TestClient
from health_history_fakes import (
    InMemoryHealthAuthorizationReader,
    InMemoryHealthHistoryStore,
    InMemoryHealthReportHistoryReader,
    authorization,
    report_entry,
)
from leak_checks import leaks
from test_api import VERIFIER, token
from test_health_history import PERMISSIONS, SENSITIVE

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.health_history import build_health_history_router
from training_management.application.queries.health_history import GetHealthHistory

ITEM_FIELDS = {
    "report_id",
    "type",
    "body_area",
    "severity",
    "occurred_at",
    "expected_recovery_days",
    "status",
    "session_id",
}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class HealthHistoryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryHealthHistoryStore()
        self.athlete_id, self.professional_id = uuid4(), uuid4()
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_health_history_router(
                    verify_token=VERIFIER.verify,
                    get_health_history=GetHealthHistory(
                        reader=InMemoryHealthReportHistoryReader(self.store),
                        authorizations=InMemoryHealthAuthorizationReader(self.store),
                        permissions=PERMISSIONS,
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.athlete = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}
        self.professional = {
            "Authorization": f"Bearer {token(self.professional_id, 'service_provider')}"
        }
        self.path = f"/v1/athletes/{self.athlete_id}/health-reports"

    def get(self, path: str, headers: dict[str, str], **params: Any) -> Any:
        return self.client.get(path, headers=headers, params=params)

    def authorize(self, *, active: bool = True) -> None:
        self.store.authorizations[(self.athlete_id, self.professional_id)] = authorization(
            self.athlete_id, self.professional_id, NOW, active=active
        )

    def test_own_reports_shape_order_and_cache(self) -> None:
        """CA1 y CA4 por HTTP."""
        empty = self.get("/v1/me/health-reports", self.athlete)
        session_id = uuid4()
        for days in (3, 1, 2):
            self.store.add(
                report_entry(self.athlete_id, NOW - timedelta(days=days), session_id=session_id)
            )

        response = self.get("/v1/me/health-reports", self.athlete, limit=2)

        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"items": [], "next_cursor": None})
        body = response.json()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(set(body["items"][0]), ITEM_FIELDS)
        occurred = [i["occurred_at"] for i in body["items"]]
        self.assertTrue(all(value.endswith("Z") for value in occurred))
        self.assertEqual(occurred, sorted(occurred, reverse=True))
        self.assertEqual(body["items"][0]["status"], "OPEN")
        self.assertEqual(body["items"][0]["session_id"], str(session_id))
        self.assertIsNotNone(body["next_cursor"])

        rest = self.get("/v1/me/health-reports", self.athlete, limit=2, cursor=body["next_cursor"])
        self.assertEqual(len(rest.json()["items"]), 1)
        self.assertIsNone(rest.json()["next_cursor"])

    def test_filters_by_type_and_severity(self) -> None:
        injury = report_entry(self.athlete_id, NOW, type="INJURY", severity="HIGH")
        self.store.add(injury, report_entry(self.athlete_id, NOW - timedelta(hours=1)))

        by_type = self.get("/v1/me/health-reports", self.athlete, type="INJURY")
        by_severity = self.get("/v1/me/health-reports", self.athlete, severity="HIGH")

        for response in (by_type, by_severity):
            self.assertEqual(
                [i["report_id"] for i in response.json()["items"]], [str(injury.report_id)]
            )

    def test_professional_route_requires_active_authorization(self) -> None:
        """CA2 y CA3 por HTTP: mismos campos que el deportista; 403 sin revelar existencia."""
        self.store.add(report_entry(self.athlete_id, NOW))
        without = self.get(self.path, self.professional)
        unknown = self.get(f"/v1/athletes/{uuid4()}/health-reports", self.professional)
        self.authorize()
        allowed = self.get(self.path, self.professional)
        by_athlete = self.get(self.path, self.athlete)
        own_by_professional = self.get("/v1/me/health-reports", self.professional)
        self.authorize(active=False)
        revoked = self.get(self.path, self.professional)

        for response in (without, unknown, revoked):
            self.assertEqual(
                (response.status_code, response.json()["code"]),
                (403, "athlete_authorization_required"),
            )
        self.assertEqual(without.json()["message"], unknown.json()["message"])
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(set(allowed.json()["items"][0]), ITEM_FIELDS)
        self.assertEqual(by_athlete.json()["code"], "permission_required")
        self.assertEqual(own_by_professional.json()["code"], "permission_required")

    def test_validation_errors(self) -> None:
        """CA6: periodo, cursor, límite o filtro inválido → 422 con el parámetro."""
        cases = (
            ({"from": "2026-10-09", "to": "2026-10-08"}, "query.to"),
            ({"cursor": "basura"}, "query.cursor"),
            ({"limit": 0}, "query.limit"),
            ({"limit": 101}, "query.limit"),
            ({"type": "FRACTURE"}, "query.type"),
            ({"severity": "CRITICAL"}, "query.severity"),
            ({"from": "ayer"}, "query.from"),
        )
        for params, field in cases:
            for path, headers in (
                ("/v1/me/health-reports", self.athlete),
                (self.path, self.professional),
            ):
                with self.subTest(field=field, path=path):
                    self.authorize()
                    response = self.get(path, headers, **params)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["code"], "validation_error")
                    self.assertEqual(response.json()["details"][0]["field"], field)

    def test_authentication_is_required(self) -> None:
        for path in ("/v1/me/health-reports", self.path):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_period_filter_is_inclusive(self) -> None:
        inside = report_entry(self.athlete_id, datetime(2026, 10, 8, 23, 59, tzinfo=UTC))
        outside = report_entry(self.athlete_id, datetime(2026, 10, 9, 0, 0, tzinfo=UTC))
        self.store.add(inside, outside)

        response = self.get(
            "/v1/me/health-reports", self.athlete, **{"from": "2026-10-08", "to": "2026-10-08"}
        )

        self.assertEqual(
            [i["report_id"] for i in response.json()["items"]], [str(inside.report_id)]
        )

    def test_health_data_never_reaches_logs_or_errors(self) -> None:
        """CA5: ningún valor de los reportes en logs, auditoría ni mensajes de error."""
        self.store.add(
            report_entry(
                self.athlete_id,
                NOW,
                type="INCAPACITY",
                body_area="ANKLE_FOOT",
                severity="MODERATE",
                expected_recovery_days=187,
            )
        )
        capture = _Capture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            rejected = self.get(self.path, self.professional)
            self.authorize()
            allowed = self.get(self.path, self.professional)
            own = self.get("/v1/me/health-reports", self.athlete)
            invalid = self.get("/v1/me/health-reports", self.athlete, cursor="basura")
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        self.assertEqual(allowed.json()["items"][0]["body_area"], "ANKLE_FOOT")
        self.assertEqual(own.status_code, 200)
        for response in (rejected, invalid):
            self.assertEqual(leaks({"body": response.text}, SENSITIVE), [])
        self.assertTrue(capture.records)
        for record in capture.records:
            fields = {
                "message": record.getMessage(),
                **getattr(record, "fields", {}),
            }
            self.assertEqual(leaks(fields, SENSITIVE), [], record.getMessage())
        audits = [r for r in capture.records if r.getMessage() == "audit.health_history_access"]
        self.assertEqual(
            [r.fields["result"] for r in audits],  # type: ignore[attr-defined]
            ["rejected:athlete_authorization_required", "granted"],
        )


if __name__ == "__main__":
    unittest.main()
