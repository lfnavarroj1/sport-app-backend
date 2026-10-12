"""HU011 con PostgreSQL real: backfill de autorizaciones, proyecciones e historial paginado."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from support import athlete_and_professional, training_databases

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.contracts import repository_root
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.errors import AthleteAuthorizationRequired
from training_management.application.messages import parse_training_event
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.queries.training_history import HistoryFilters
from training_management.domain.authorizations.events import authorization_aggregate_id
from training_management.domain.plans.model import Sport
from training_management.infrastructure.persistence_read.models import (
    TrainingAuthorizationViewRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_write.models import OutboxEventRow
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import OutboxPublisherSettings
from training_management.workers.outbox_publisher import OutboxRelay

WRITE_HISTORY = "training_management.infrastructure.persistence_write:migrations"
SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"


def schema(event_type: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    )


def session_envelope(
    athlete_id: UUID, ended_at: datetime, sport: str = "running"
) -> dict[str, Any]:
    session_id = str(uuid4())
    return {
        "event_id": str(uuid4()),
        "event_type": "entrenamiento.finalizado.v1",
        "occurred_at": ended_at.isoformat(),
        "producer": "training-management",
        "aggregate_type": "training_session",
        "aggregate_id": session_id,
        "aggregate_version": 2,
        "correlation_id": None,
        "causation_id": None,
        "schema_version": 1,
        "payload": {
            "session_id": session_id,
            "athlete_id": str(athlete_id),
            "plan_id": None,
            "sport": sport,
            "started_at": (ended_at - timedelta(minutes=30)).isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": 1800,
            "status": "FINISHED",
        },
    }


class AuthorizationBackfillTest(unittest.TestCase):
    def test_existing_authorizations_get_one_event_each(self) -> None:
        """Las autorizaciones creadas por HU010 sin eventos se publican con su estado actual."""
        with temporary_database("hu011_backfill") as url:
            upgrade(WRITE_HISTORY, url, revision="0002_training_plans")
            engine = create_engine(url)
            try:
                active = (uuid4(), uuid4())
                revoked = (uuid4(), uuid4())
                now = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
                with engine.begin() as connection:
                    for (athlete, professional), status, version in (
                        (active, "ACTIVE", 3),
                        (revoked, "REVOKED", 2),
                    ):
                        connection.execute(
                            text(
                                "INSERT INTO training_authorization VALUES "
                                "(:a, :p, :s, :g, :r, :u, :v)"
                            ),
                            {
                                "a": athlete,
                                "p": professional,
                                "s": status,
                                "g": now,
                                "r": now if status == "REVOKED" else None,
                                "u": now,
                                "v": version,
                            },
                        )

                upgrade(WRITE_HISTORY, url)

                with sessionmaker(engine)() as session:
                    events = list(session.scalars(select(OutboxEventRow)))
            finally:
                engine.dispose()

        by_aggregate = {event.aggregate_id: event for event in events}
        self.assertEqual(len(events), 2)
        granted = by_aggregate[authorization_aggregate_id(*active)]
        revocation = by_aggregate[authorization_aggregate_id(*revoked)]
        self.assertEqual(
            (granted.event_type, granted.aggregate_version),
            ("autorizacion_entrenamiento.otorgada.v1", 3),
        )
        self.assertEqual(
            (revocation.event_type, revocation.aggregate_version),
            ("autorizacion_entrenamiento.revocada.v1", 2),
        )
        self.assertEqual(granted.causation_id, "migration:0003_authorization_backfill")
        # Cada evento del backfill es un sobre válido una vez publicado y lo acepta el proyector.
        for event in events:
            envelope = {
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "producer": "training-management",
                "aggregate_type": event.aggregate_type,
                "aggregate_id": str(event.aggregate_id),
                "aggregate_version": event.aggregate_version,
                "correlation_id": None,
                "causation_id": event.causation_id,
                "schema_version": event.schema_version,
                "payload": event.payload,
            }
            schema(event.event_type).validate(envelope)
            parse_training_event(envelope)


class HistoryProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(training_databases("hu011"))
        self.athlete, self.professional = athlete_and_professional()

    def relay(self) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        class Capture:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                sent.append(envelope)

        OutboxRelay(
            SqlAlchemyOutboxStore(self.db.writes),
            Capture(),
            OutboxPublisherSettings(
                app_env="test",  # type: ignore[arg-type]
                database_write_url=self.db.write_url,  # type: ignore[arg-type]
                pubsub_project_id="sportapp-test",
                worker_id="w-test",
            ),
        ).run_once()
        return sent

    def project_all(self, envelopes: list[dict[str, Any]]) -> None:
        project = self.db.project()
        for envelope in envelopes:
            project(parse_training_event(envelope))

    def test_authorization_flows_from_outbox_to_read_model(self) -> None:
        """CA3: otorgar habilita la consulta; revocar la rechaza al proyectarse."""
        history = self.db.history()
        self.db.grant()(self.athlete, self.professional.user_id)
        granted = self.relay()
        for envelope in granted:
            schema(envelope["event_type"]).validate(envelope)
        self.project_all(granted)

        page = history.of_athlete(self.professional, self.athlete.user_id, HistoryFilters())

        self.db.revoke()(self.athlete, self.professional.user_id)
        self.project_all(self.relay())
        with self.assertRaises(AthleteAuthorizationRequired):
            history.of_athlete(self.professional, self.athlete.user_id, HistoryFilters())
        self.assertEqual(page.items, ())
        with self.db.reads() as session:
            row = session.scalars(select(TrainingAuthorizationViewRow)).one()
        self.assertEqual((row.status, row.version), ("REVOKED", 2))

    def test_out_of_order_and_duplicate_authorization_events(self) -> None:
        self.db.grant()(self.athlete, self.professional.user_id)
        self.db.revoke()(self.athlete, self.professional.user_id)
        self.db.grant()(self.athlete, self.professional.user_id)
        envelopes = self.relay()

        project = self.db.project()
        outcomes = [project(parse_training_event(e)) for e in reversed(envelopes)]
        outcomes.append(project(parse_training_event(envelopes[-1])))

        self.assertEqual(
            outcomes,
            [
                ProjectionOutcome.PROJECTED,
                ProjectionOutcome.STALE,
                ProjectionOutcome.STALE,
                ProjectionOutcome.DUPLICATE,
            ],
        )
        with self.db.reads() as session:
            row = session.scalars(select(TrainingAuthorizationViewRow)).one()
        self.assertEqual((row.status, row.version), ("ACTIVE", 3))

    def test_concurrent_duplicate_session_deliveries(self) -> None:
        event = parse_training_event(session_envelope(self.athlete.user_id, datetime.now(UTC)))
        project = self.db.project()
        barrier = threading.Barrier(6)

        def deliver(_: int) -> ProjectionOutcome:
            barrier.wait()
            return project(event)

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(deliver, range(6)))

        self.assertEqual(outcomes.count(ProjectionOutcome.PROJECTED), 1)
        self.assertEqual(self.db.count(TrainingSessionHistoryRow, read=True), 1)

    def test_paginated_history_with_filters_against_postgres(self) -> None:
        """CA1, CA2 y CA4 con el índice y el keyset reales."""
        start = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
        tie = start + timedelta(days=3)
        envelopes = [
            session_envelope(self.athlete.user_id, start + timedelta(hours=i)) for i in range(50)
        ]
        envelopes += [session_envelope(self.athlete.user_id, tie, "swimming") for _ in range(7)]
        envelopes.append(session_envelope(uuid4(), start))  # de otro deportista
        self.project_all(envelopes)
        history = self.db.history()
        mine = {UUID(e["aggregate_id"]) for e in envelopes[:-1]}

        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = history.own(self.athlete, HistoryFilters(limit=20, cursor=cursor))
            ended = [item.ended_at for item in page.items]
            self.assertEqual(ended, sorted(ended, reverse=True))
            seen.extend(item.session_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        swimming = history.own(self.athlete, HistoryFilters(sport=Sport.SWIMMING, limit=100))
        one_day = history.own(
            self.athlete,
            HistoryFilters(date_from=date(2026, 9, 2), date_to=date(2026, 9, 2), limit=100),
        )
        stranger = history.own(athlete_and_professional()[0], HistoryFilters())

        self.assertEqual(len(seen), len(set(seen)), "registro repetido entre páginas")
        self.assertEqual(set(seen), mine, "registro perdido o ajeno")
        self.assertEqual(len(swimming.items), 7)
        self.assertEqual(len(one_day.items), 24)
        self.assertEqual((stranger.items, stranger.next_cursor), ((), None))


if __name__ == "__main__":
    unittest.main()
