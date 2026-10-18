"""HU011 (CA5) con PostgreSQL real: deportistas vigentes del profesional en la vista proyectada."""

import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import inspect
from support import HistoryViewer, training_databases

from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller
from training_management.application.queries.authorized_athletes import GetAuthorizedAthletes
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyAuthorizedAthleteReader,
)
from training_management.infrastructure.persistence_read.models import (
    TrainingAuthorizationViewRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import OutboxPublisherSettings
from training_management.workers.outbox_publisher import OutboxRelay


class AuthorizedAthletesPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(training_databases("hu011_ca5"))
        self.professional = Caller(uuid4(), "service_provider")
        self.query = GetAuthorizedAthletes(
            reader=SqlAlchemyAuthorizedAthleteReader(self.db.reads), permissions=HistoryViewer()
        )

    def relay_and_project(self) -> None:
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
        project = self.db.project()
        for envelope in sent:
            project(parse_training_event(envelope))

    def ids(self, **kwargs: Any) -> list[UUID]:
        return [item.athlete_id for item in self.query(self.professional, **kwargs).items]

    def test_grant_and_revoke_flow_through_the_projection(self) -> None:
        """Otorgar agrega al deportista; revocar lo retira cuando se proyecta (CA5)."""
        first, second = Caller(uuid4(), "athlete"), Caller(uuid4(), "athlete")
        stranger = Caller(uuid4(), "athlete")
        self.db.grant()(first, self.professional.user_id)
        self.db.grant()(second, self.professional.user_id)
        self.db.grant()(stranger, uuid4())

        before_projection = self.ids()
        self.relay_and_project()
        granted = self.ids()
        self.db.revoke()(first, self.professional.user_id)
        not_yet_projected = self.ids()
        self.relay_and_project()

        self.assertEqual(before_projection, [])
        self.assertEqual(granted, [second.user_id, first.user_id])
        self.assertEqual(not_yet_projected, granted)
        self.assertEqual(self.ids(), [second.user_id])

    def test_keyset_pagination_with_ties_and_only_active_rows(self) -> None:
        same = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
        newest = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)
        tied = sorted((uuid4() for _ in range(3)), reverse=True)
        latest, revoked = uuid4(), uuid4()
        rows = [(athlete, same, "ACTIVE") for athlete in tied]
        rows += [(latest, newest, "ACTIVE"), (revoked, newest, "REVOKED")]
        with self.db.reads() as session, session.begin():
            for athlete_id, granted_at, status in rows:
                session.add(
                    TrainingAuthorizationViewRow(
                        athlete_id=athlete_id,
                        professional_id=self.professional.user_id,
                        status=status,
                        version=1,
                        granted_at=granted_at,
                        revoked_at=None,
                        projected_at=newest,
                    )
                )

        pages: list[list[UUID]] = []
        cursor: str | None = None
        while True:
            page = self.query(self.professional, limit=2, cursor=cursor)
            pages.append([item.athlete_id for item in page.items])
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(pages, [[latest, tied[0]], [tied[1], tied[2]]])
        self.assertEqual(self.query(self.professional, limit=2).items[0].authorized_at, newest)

    def test_index_supports_the_professional_order(self) -> None:
        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(self.db.read_engine).get_indexes("training_authorization_view")
        }

        self.assertEqual(
            indexes["ix_training_authorization_view_professional_order"],
            ["professional_id", "status", "granted_at", "athlete_id"],
        )


if __name__ == "__main__":
    unittest.main()
