"""HU042 CA2: catálogos, rangos, fechas y consentimiento del reporte de salud (ADR-037)."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid1, uuid4

from fakes import NOW
from health_fakes import CONSENT, health_body

from training_management.application.health_documents import (
    InvalidHealthReportDocument,
    health_report_document,
    health_report_from_document,
)
from training_management.domain.health.model import (
    BodyArea,
    HealthReport,
    ReportStatus,
    ReportType,
    Severity,
    validate_report,
)
from training_management.domain.health.validation import InvalidHealthReport, Issue

AREAS = (
    "HEAD",
    "NECK",
    "SHOULDER",
    "ARM",
    "ELBOW",
    "WRIST_HAND",
    "CHEST",
    "BACK",
    "HIP",
    "THIGH",
    "KNEE",
    "LOWER_LEG",
    "ANKLE_FOOT",
    "OTHER",
)


def violations(body: Any) -> set[tuple[str | None, str]]:
    try:
        validate_report(body, NOW, CONSENT)
    except InvalidHealthReport as error:
        return {(item.field, item.issue.value) for item in error.violations}
    raise AssertionError("se esperaba InvalidHealthReport")


class CatalogTest(unittest.TestCase):
    def test_catalogs_are_the_closed_lists_of_adr_037(self) -> None:
        self.assertEqual(
            [item.value for item in ReportType], ["DISCOMFORT", "INJURY", "INCAPACITY"]
        )
        self.assertEqual(tuple(item.value for item in BodyArea), AREAS)
        self.assertEqual([item.value for item in Severity], ["LOW", "MODERATE", "HIGH"])

    def test_every_catalog_value_is_accepted(self) -> None:
        for report_type in ReportType:
            for severity in Severity:
                for area in BodyArea:
                    data = validate_report(
                        health_body(
                            type=report_type.value, severity=severity.value, body_area=area.value
                        ),
                        NOW,
                        CONSENT,
                    )
                    self.assertEqual(
                        (data.type, data.severity, data.body_area), (report_type, severity, area)
                    )

    def test_values_outside_the_catalog_are_unknown_value(self) -> None:
        body = health_body(type="FRACTURE", body_area="knee", severity=3)

        self.assertEqual(
            violations(body),
            {
                ("type", "unknown_value"),
                ("body_area", "unknown_value"),
                ("severity", "unknown_value"),
            },
        )


class ValidReportTest(unittest.TestCase):
    def test_normalizes_a_complete_report(self) -> None:
        session_id = uuid4()
        body = health_body(session_id=str(session_id), expected_recovery_days=0)

        data = validate_report(body, NOW, CONSENT)

        self.assertEqual(data.report_id, UUID(body["report_id"]))
        self.assertEqual(data.session_id, session_id)
        self.assertEqual(data.expected_recovery_days, 0)
        self.assertEqual(data.occurred_at, NOW - timedelta(minutes=90))
        self.assertEqual(data.consent_version, CONSENT)

    def test_optional_fields_can_be_absent_or_null(self) -> None:
        body = health_body(session_id=None)
        del body["expected_recovery_days"]

        data = validate_report(body, NOW, CONSENT)

        self.assertIsNone(data.session_id)
        self.assertIsNone(data.expected_recovery_days)

    def test_registered_report_is_open_in_version_one(self) -> None:
        data = validate_report(health_body(), NOW, CONSENT)
        athlete_id = uuid4()

        report = HealthReport.register(athlete_id=athlete_id, data=data, now=NOW)

        self.assertEqual(report.status, ReportStatus.OPEN)
        self.assertEqual(report.version, 1)
        self.assertEqual(report.reported_at, NOW)
        self.assertTrue(report.matches(athlete_id, data))
        self.assertFalse(report.matches(uuid4(), data))

    def test_document_round_trip(self) -> None:
        data = validate_report(health_body(session_id=str(uuid4())), NOW, CONSENT)
        report = HealthReport.register(athlete_id=uuid4(), data=data, now=NOW)

        document = health_report_document(report)

        self.assertEqual(health_report_from_document(document), report)
        self.assertEqual(document["status"], "OPEN")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["occurred_at"], "2026-10-12T10:30:00Z")
        self.assertEqual(document["consent"], {"version": CONSENT})

    def test_corrupt_document_is_rejected_without_data(self) -> None:
        document = health_report_document(
            HealthReport.register(
                athlete_id=uuid4(), data=validate_report(health_body(), NOW, CONSENT), now=NOW
            )
        )

        for field, value, reason in (
            ("severity", "CRITICAL", "severity_invalid"),
            ("status", "CLOSED", "status_invalid"),
            ("expected_recovery_days", 400, "expected_recovery_days_invalid"),
            ("consent", {}, "consent_invalid"),
            ("version", 0, "version_invalid"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(InvalidHealthReportDocument) as raised,
            ):
                health_report_from_document({**document, field: value})
            self.assertEqual(raised.exception.reason, reason)


class RangesAndDatesTest(unittest.TestCase):
    def test_recovery_days_bounds(self) -> None:
        for value in (0, 365):
            data = validate_report(health_body(expected_recovery_days=value), NOW, CONSENT)
            self.assertEqual(data.expected_recovery_days, value)
        self.assertEqual(
            violations(health_body(expected_recovery_days=-1)),
            {("expected_recovery_days", "out_of_range")},
        )
        self.assertEqual(
            violations(health_body(expected_recovery_days=366)),
            {("expected_recovery_days", "out_of_range")},
        )
        for value in ("3", 2.5, True):
            with self.subTest(value=value):
                self.assertEqual(
                    violations(health_body(expected_recovery_days=value)),
                    {("expected_recovery_days", "unknown_value")},
                )

    def test_occurred_at_window(self) -> None:
        limit_future = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        limit_past = (NOW - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for value in (limit_future, limit_past):
            validate_report(health_body(occurred_at=value), NOW, CONSENT)
        future = (NOW + timedelta(minutes=5, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (NOW - timedelta(days=365, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.assertEqual(
            violations(health_body(occurred_at=future)), {("occurred_at", "too_far_in_future")}
        )
        self.assertEqual(violations(health_body(occurred_at=old)), {("occurred_at", "too_old")})

    def test_occurred_at_must_be_utc_z_in_seconds(self) -> None:
        for value in (
            "2026-10-12T10:30:00+00:00",
            "2026-10-12T10:30:00.123Z",
            "2026-10-12",
            "2026-02-30T10:30:00Z",
            1760265000,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    violations(health_body(occurred_at=value)),
                    {("occurred_at", "invalid_datetime")},
                )


class ConsentTest(unittest.TestCase):
    def test_consent_is_required(self) -> None:
        body = health_body()
        del body["consent"]

        self.assertEqual(violations(body), {("consent.version", "required")})
        self.assertEqual(violations(health_body(consent={})), {("consent.version", "required")})

    def test_other_consent_version_is_rejected(self) -> None:
        self.assertEqual(
            violations(health_body(consent={"version": "old-v0"})),
            {("consent.version", "unsupported_consent_version")},
        )

    def test_malformed_consent(self) -> None:
        self.assertEqual(violations(health_body(consent="yes")), {("consent", "unknown_value")})
        self.assertEqual(
            violations(health_body(consent={"version": 1})), {("consent.version", "unknown_value")}
        )
        self.assertEqual(
            violations(health_body(consent={"version": CONSENT, "accepted": True})),
            {("consent.accepted", "field_not_allowed")},
        )


class ShapeTest(unittest.TestCase):
    def test_identity_state_and_free_text_are_not_allowed(self) -> None:
        body = health_body(
            athlete_id=str(uuid4()), status="OPEN", version=1, notes="me duele", diagnosis="x"
        )

        self.assertEqual(
            violations(body),
            {
                ("athlete_id", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("version", "field_not_allowed"),
                ("notes", "field_not_allowed"),
                ("diagnosis", "field_not_allowed"),
            },
        )

    def test_required_fields(self) -> None:
        self.assertEqual(
            violations({"consent": {"version": CONSENT}}),
            {
                ("report_id", "required"),
                ("type", "required"),
                ("body_area", "required"),
                ("severity", "required"),
                ("occurred_at", "required"),
            },
        )

    def test_identifiers_must_be_uuid_v4(self) -> None:
        body = health_body(report_id=str(uuid1()), session_id="sesion-1")

        self.assertEqual(
            violations(body), {("report_id", "invalid_uuid"), ("session_id", "invalid_uuid")}
        )

    def test_body_must_be_an_object(self) -> None:
        self.assertEqual(violations(None), {(None, "required")})
        self.assertEqual(violations([health_body()]), {(None, "unknown_value")})

    def test_all_issues_belong_to_the_closed_catalog(self) -> None:
        self.assertEqual(
            {item.value for item in Issue},
            {
                "required",
                "field_not_allowed",
                "unknown_value",
                "invalid_uuid",
                "invalid_datetime",
                "too_far_in_future",
                "too_old",
                "out_of_range",
                "unsupported_consent_version",
            },
        )


if __name__ == "__main__":
    unittest.main()
