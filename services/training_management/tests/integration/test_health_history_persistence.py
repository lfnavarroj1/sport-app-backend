"""HU012 con PostgreSQL real: reportes de HU042 proyectados a `health_report_view` y consulta
paginada por keyset; autorización proyectada de HU010/HU011 vigente, revocada y ajena; auditoría
del acceso del profesional (ADR-037, CA1-CA4, CA6)."""

import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from support import TrainingDatabases, athlete_and_professional, training_databases, utc_seconds

from training_management.application.commands.report_health import (
    ReportHealth,
    ReportHealthCommand,
)
from training_management.application.errors import AthleteAuthorizationRequired
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller
from training_management.application.queries.health_history import (
    GetHealthHistory,
    HealthHistoryFilters,
)
from training_management.domain.health.model import ReportType, Severity
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.health_history import (
    SqlAlchemyHealthReportHistoryReader,
)
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyAuthorizationReader,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyHealthReportUnitOfWork,
)
from training_management.workers.outbox_publisher import build_envelope

CONSENT = "local-dev"
REPORTS = 57


class HealthViewer:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "health.history.view_assigned"


class HealthHistoryPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu012"))
        self.athlete, self.professional = athlete_and_professional()
        self.report = ReportHealth(
            unit_of_work=lambda: SqlAlchemyHealthReportUnitOfWork(self.db.writes),
            clock=SystemClock(),
            consent_version=CONSENT,
        )
        self.history = GetHealthHistory(
            reader=SqlAlchemyHealthReportHistoryReader(self.db.reads),
            authorizations=SqlAlchemyAuthorizationReader(self.db.reads),
            permissions=HealthViewer(),
        )

    def register(self, athlete: Caller, **overrides: Any) -> UUID:
        document: dict[str, Any] = {
            "report_id": str(uuid4()),
            "type": "DISCOMFORT",
            "body_area": "KNEE",
            "severity": "LOW",
            "occurred_at": utc_seconds(-timedelta(hours=2)),
            "consent": {"version": CONSENT},
            **overrides,
        }
        self.report(
            athlete,
            ReportHealthCommand(
                body=document, idempotency_key=str(uuid4()), correlation_id=uuid4()
            ),
        )
        return UUID(document["report_id"])

    def project_pending(self) -> None:
        """Publica la outbox y aplica los eventos con el proyector real de Entrenamientos."""
        store = SqlAlchemyOutboxStore(self.db.writes)
        project = self.db.project()
        while claimed := store.claim_batch("w-test", 100, 30):
            for event in claimed:
                store.mark_published(event.event_id, "w-test")
                project(parse_training_event(build_envelope(event)))

    def test_keyset_pages_57_projected_reports_without_losses_or_repeats(self) -> None:
        """CA1 y CA6: 57 reportes (con empates de instante) en páginas de 20 por PostgreSQL."""
        tied = utc_seconds(-timedelta(days=3))
        expected = {self.register(self.athlete, occurred_at=tied) for _ in range(7)}
        expected |= {
            self.register(self.athlete, occurred_at=utc_seconds(-timedelta(hours=5 * i + 1)))
            for i in range(REPORTS - 7)
        }
        self.register(Caller(uuid4(), "athlete"))
        self.project_pending()

        seen: list[UUID] = []
        instants: list[tuple[datetime, UUID]] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = self.history.own(self.athlete, HealthHistoryFilters(limit=20, cursor=cursor))
            seen.extend(item.report_id for item in page.items)
            instants.extend((item.occurred_at, item.report_id) for item in page.items)
            pages += 1
            if pages == 1:
                # Un reporte nuevo y más reciente entre páginas no altera las siguientes.
                self.register(self.athlete, occurred_at=utc_seconds())
                self.project_pending()
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), REPORTS)
        self.assertEqual(len(seen), len(set(seen)), "reporte repetido")
        self.assertEqual(set(seen), expected, "reporte perdido o ajeno")
        self.assertEqual(instants, sorted(instants, reverse=True))

    def test_filters_by_inclusive_period_type_and_severity(self) -> None:
        """CA1: fechas UTC inclusivas, tipo y severidad sobre la vista proyectada."""
        today = datetime.now(UTC).date()
        start_of_today = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        first = self.register(
            self.athlete,
            occurred_at=(start_of_today - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        last = self.register(
            self.athlete,
            occurred_at=(start_of_today - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            type="INJURY",
            severity="HIGH",
        )
        older = self.register(
            self.athlete,
            occurred_at=(start_of_today - timedelta(days=2, seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )
        self.project_pending()
        period = HealthHistoryFilters(
            date_from=today - timedelta(days=2), date_to=today - timedelta(days=1)
        )

        in_period = self.history.own(self.athlete, period)
        injuries = self.history.own(self.athlete, HealthHistoryFilters(type=ReportType.INJURY))
        high = self.history.own(self.athlete, HealthHistoryFilters(severity=Severity.HIGH))
        empty = self.history.own(self.athlete, HealthHistoryFilters(date_to=date(2000, 1, 1)))

        self.assertEqual([i.report_id for i in in_period.items], [last, first])
        self.assertNotIn(older, [i.report_id for i in in_period.items])
        self.assertEqual([i.report_id for i in injuries.items], [last])
        self.assertEqual([i.report_id for i in high.items], [last])
        self.assertEqual((empty.items, empty.next_cursor), ((), None))

    def test_professional_access_follows_the_projected_authorization(self) -> None:
        """CA2 y CA3: vigente → mismos campos; ajena, revocada o inexistente → 403 auditado."""
        session_report = self.register(self.athlete, expected_recovery_days=10)
        stranger = Caller(uuid4(), "service_provider")
        self.project_pending()

        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(
                    self.professional, self.athlete.user_id, HealthHistoryFilters()
                )
            self.db.grant()(self.athlete, self.professional.user_id)
            self.project_pending()
            page = self.history.of_athlete(
                self.professional, self.athlete.user_id, HealthHistoryFilters()
            )
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(stranger, self.athlete.user_id, HealthHistoryFilters())
            self.db.revoke()(self.athlete, self.professional.user_id)
            # La revocación surte efecto al proyectarse (riesgo aceptado como en HU011).
            before_projection = self.history.of_athlete(
                self.professional, self.athlete.user_id, HealthHistoryFilters()
            )
            self.project_pending()
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(
                    self.professional, self.athlete.user_id, HealthHistoryFilters()
                )

        self.assertEqual([i.report_id for i in page.items], [session_report])
        item = page.items[0]
        self.assertEqual(
            (item.type, item.body_area, item.severity, item.status, item.expected_recovery_days),
            ("DISCOMFORT", "KNEE", "LOW", "OPEN", 10),
        )
        self.assertEqual(len(before_projection.items), 1)
        # Otorgar y revocar (HU010) también auditan; solo cuentan los accesos al historial.
        fields = [
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.getMessage() == "audit.health_history_access"
        ]
        self.assertEqual(
            [(f["result"], f["professional_id"]) for f in fields],
            [
                ("rejected:athlete_authorization_required", str(self.professional.user_id)),
                ("granted", str(self.professional.user_id)),
                ("rejected:athlete_authorization_required", str(stranger.user_id)),
                ("granted", str(self.professional.user_id)),
                ("rejected:athlete_authorization_required", str(self.professional.user_id)),
            ],
        )
        for record in fields:
            self.assertEqual(set(record), {"result", "professional_id", "actor_type", "athlete_id"})


if __name__ == "__main__":
    unittest.main()
