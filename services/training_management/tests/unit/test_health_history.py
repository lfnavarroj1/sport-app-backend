"""HU012 sin infraestructura: consulta del historial de reportes de salud (ADR-037)."""

import base64
import unittest
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fakes import NOW, FakePermissions
from health_history_fakes import (
    InMemoryHealthAuthorizationReader,
    InMemoryHealthHistoryStore,
    InMemoryHealthReportHistoryReader,
    authorization,
    report_entry,
)
from leak_checks import leaks

from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.ports import Caller
from training_management.application.queries.health_history import (
    GetHealthHistory,
    HealthHistoryFilters,
    InvalidHealthHistoryQuery,
    decode_cursor,
)
from training_management.domain.health.model import ReportType, Severity

HEALTH = "health.history.view_assigned"
PERMISSIONS = FakePermissions(
    {
        "service_provider": {"training.plan.author", "training.history.view_assigned", HEALTH},
        "organizer": set(),
    }
)
# Valores sintéticos distinguibles para comprobar que no aparecen en la auditoría (CA5); sin
# cifras, que podrían coincidir con `duration_ms` o un UUID.
SENSITIVE = ("INCAPACITY", "ANKLE_FOOT", "MODERATE")


class HealthHistoryQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryHealthHistoryStore()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional = Caller(uuid4(), "service_provider")
        self.query = GetHealthHistory(
            reader=InMemoryHealthReportHistoryReader(self.store),
            authorizations=InMemoryHealthAuthorizationReader(self.store),
            permissions=PERMISSIONS,
        )

    def authorize(self, *, active: bool = True) -> None:
        self.store.authorizations[(self.athlete.user_id, self.professional.user_id)] = (
            authorization(self.athlete.user_id, self.professional.user_id, NOW, active=active)
        )

    def test_athlete_filters_by_period_type_and_severity_in_date_order(self) -> None:
        """CA1: solo los suyos, por periodo, tipo o severidad, del más reciente al más antiguo."""
        day = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
        mine = [report_entry(self.athlete.user_id, day + timedelta(days=i)) for i in range(5)]
        injury = report_entry(
            self.athlete.user_id, day + timedelta(days=2, hours=3), type="INJURY", severity="HIGH"
        )
        other = report_entry(uuid4(), day + timedelta(days=1))
        self.store.add(*mine, injury, other)

        page = self.query.own(
            self.athlete,
            HealthHistoryFilters(date_from=date(2026, 10, 6), date_to=date(2026, 10, 8)),
        )
        by_type = self.query.own(self.athlete, HealthHistoryFilters(type=ReportType.INJURY))
        by_severity = self.query.own(self.athlete, HealthHistoryFilters(severity=Severity.HIGH))
        combined = self.query.own(
            self.athlete, HealthHistoryFilters(type=ReportType.INJURY, severity=Severity.LOW)
        )

        self.assertEqual(
            [item.report_id for item in page.items],
            [mine[3].report_id, injury.report_id, mine[2].report_id, mine[1].report_id],
        )
        self.assertNotIn(other.report_id, {item.report_id for item in page.items})
        self.assertEqual([item.report_id for item in by_type.items], [injury.report_id])
        self.assertEqual([item.report_id for item in by_severity.items], [injury.report_id])
        self.assertEqual(combined.items, ())

    def test_reports_are_isolated_per_athlete(self) -> None:
        someone = Caller(uuid4(), "athlete")
        self.store.add(report_entry(someone.user_id, NOW))

        self.assertEqual(self.query.own(self.athlete, HealthHistoryFilters()).items, ())
        self.assertEqual(len(self.query.own(someone, HealthHistoryFilters()).items), 1)

    def test_no_reports_returns_explicit_empty_collection(self) -> None:
        """CA4."""
        page = self.query.own(self.athlete, HealthHistoryFilters())

        self.assertEqual((page.items, page.next_cursor), ((), None))

    def test_period_bounds_are_inclusive_utc_days(self) -> None:
        first = report_entry(self.athlete.user_id, datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
        last = report_entry(self.athlete.user_id, datetime(2026, 10, 8, 23, 59, 59, tzinfo=UTC))
        before = report_entry(self.athlete.user_id, datetime(2026, 10, 7, 23, 59, tzinfo=UTC))
        after = report_entry(self.athlete.user_id, datetime(2026, 10, 9, 0, 0, tzinfo=UTC))
        self.store.add(first, last, before, after)

        page = self.query.own(
            self.athlete,
            HealthHistoryFilters(date_from=date(2026, 10, 8), date_to=date(2026, 10, 8)),
        )

        self.assertEqual([i.report_id for i in page.items], [last.report_id, first.report_id])

    def test_professional_needs_permission_and_active_authorization(self) -> None:
        """CA2 y CA3: sin autorización, revocada o ajena → 403; otro actor → 403."""
        filters = HealthHistoryFilters()
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        self.authorize(active=False)
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        # La autorización de otro profesional no sirve a este.
        stranger = Caller(uuid4(), "service_provider")
        self.authorize()
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(stranger, self.athlete.user_id, filters)
        # Un deportista inexistente responde igual que uno sin autorización.
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, uuid4(), filters)
        for caller in (Caller(uuid4(), "organizer"), Caller(uuid4(), "athlete")):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.query.of_athlete(caller, self.athlete.user_id, filters)
        with self.assertRaises(PermissionRequired):
            self.query.own(self.professional, filters)

        self.store.add(report_entry(self.athlete.user_id, NOW))
        page = self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        self.assertEqual(len(page.items), 1)

    def test_revocation_takes_effect_once_projected(self) -> None:
        """CA2: la revocación surte efecto al proyectarse."""
        self.authorize()
        self.store.add(report_entry(self.athlete.user_id, NOW))
        self.query.of_athlete(self.professional, self.athlete.user_id, HealthHistoryFilters())

        self.authorize(active=False)

        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, self.athlete.user_id, HealthHistoryFilters())

    def test_professional_access_and_rejections_are_audited_without_health_data(self) -> None:
        """CA3 y CA5."""
        self.store.add(
            report_entry(
                self.athlete.user_id,
                NOW,
                type="INCAPACITY",
                body_area="ANKLE_FOOT",
                severity="MODERATE",
                expected_recovery_days=187,
            )
        )
        organizer = Caller(uuid4(), "organizer")
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(AthleteAuthorizationRequired):
                self.query.of_athlete(
                    self.professional, self.athlete.user_id, HealthHistoryFilters()
                )
            with self.assertRaises(PermissionRequired):
                self.query.of_athlete(organizer, self.athlete.user_id, HealthHistoryFilters())
            self.authorize()
            self.query.of_athlete(self.professional, self.athlete.user_id, HealthHistoryFilters())

        self.assertEqual(
            [record.getMessage() for record in logs.records], ["audit.health_history_access"] * 3
        )
        fields = [record.fields for record in logs.records]  # type: ignore[attr-defined]
        self.assertEqual(
            [item["result"] for item in fields],
            [
                "rejected:athlete_authorization_required",
                "rejected:permission_required",
                "granted",
            ],
        )
        self.assertEqual({item["athlete_id"] for item in fields}, {str(self.athlete.user_id)})
        self.assertEqual(fields[1]["professional_id"], str(organizer.user_id))
        for item in fields:
            self.assertEqual(leaks(item, SENSITIVE), [])

    def test_athlete_own_query_is_not_audited(self) -> None:
        with self.assertNoLogs("sportapp.audit", level="INFO"):
            self.query.own(self.athlete, HealthHistoryFilters())

    def test_pagination_never_loses_or_repeats_reports(self) -> None:
        """CA6: 45 reportes en páginas de 20, con reportes nuevos llegando entre páginas."""
        start = datetime(2026, 9, 1, tzinfo=UTC)
        same_instant = start + timedelta(days=10)
        entries = [
            report_entry(self.athlete.user_id, start + timedelta(hours=i)) for i in range(40)
        ]
        # Empates en `occurred_at`: el orden se desempata por `report_id`.
        entries += [report_entry(self.athlete.user_id, same_instant) for _ in range(5)]
        self.store.add(*entries)

        seen: list[UUID] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = self.query.own(self.athlete, HealthHistoryFilters(limit=20, cursor=cursor))
            seen.extend(item.report_id for item in page.items)
            pages += 1
            if pages == 1:
                # Llega un reporte más reciente mientras se pagina: no altera las siguientes.
                self.store.add(
                    report_entry(self.athlete.user_id, datetime(2026, 12, 1, tzinfo=UTC))
                )
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), len(set(seen)), "reporte repetido")
        self.assertEqual(set(seen), {item.report_id for item in entries}, "reporte perdido")

    def test_invalid_period_cursor_and_limit(self) -> None:
        """CA6: periodo o cursor inválido → error de validación con el parámetro."""
        # Instante sin zona horaria: el cursor no es uno emitido por el servicio.
        naive = base64.urlsafe_b64encode(
            b'{"o":"2026-10-01T00:00:00","r":"' + str(uuid4()).encode() + b'"}'
        ).decode()
        cases = (
            (HealthHistoryFilters(date_from=date(2026, 10, 9), date_to=date(2026, 10, 8)), "to"),
            (HealthHistoryFilters(cursor="no-es-un-cursor"), "cursor"),
            (HealthHistoryFilters(cursor=naive), "cursor"),
            (HealthHistoryFilters(limit=0), "limit"),
            (HealthHistoryFilters(limit=101), "limit"),
        )
        for filters, field in cases:
            with self.subTest(field=field), self.assertRaises(InvalidHealthHistoryQuery) as raised:
                self.query.own(self.athlete, filters)
            self.assertEqual(raised.exception.field, field)
            self.assertEqual(raised.exception.code, "validation_error")

    def test_cursor_round_trip(self) -> None:
        self.store.add(
            *[report_entry(self.athlete.user_id, NOW - timedelta(hours=i)) for i in range(3)]
        )

        page = self.query.own(self.athlete, HealthHistoryFilters(limit=2))

        assert page.next_cursor is not None
        position = decode_cursor(page.next_cursor)
        self.assertEqual(position.report_id, page.items[-1].report_id)
        self.assertEqual(position.occurred_at, page.items[-1].occurred_at)
        with self.assertRaises(InvalidHealthHistoryQuery):
            decode_cursor(page.next_cursor[:-4] + "!!!!")


if __name__ == "__main__":
    unittest.main()
