"""HU024 contra PostgreSQL real: transición atómica, concurrencia, restricción de estado,
eventos propios por keyset, proyección por estado completo y reconstrucción (CA1 a CA6)."""

import threading
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from support import (
    EventDatabases,
    event_command,
    event_databases,
    publish_command,
)

from events_management.application.commands.publish_event import PublicationResult
from events_management.application.errors import IdempotencyKeyReused
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import Caller, PublishedEventsQuery
from events_management.application.projections.event_projection import ProjectionOutcome
from events_management.application.queries.my_events import MyEventsFilters
from events_management.domain.sport_events.model import EventNotFound
from events_management.domain.validation import InvalidSportEvent
from events_management.infrastructure.persistence_read.models import (
    EventReadRow,
    ProcessedEventRow,
)
from events_management.infrastructure.persistence_write.models import (
    CommandIdempotencyRow,
    EventRow,
    OutboxEventRow,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.workers.outbox_publisher import build_envelope
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database

WRITE_HISTORY = "events_management.infrastructure.persistence_write:migrations"


def concurrently(count: int, action: Callable[[], Any]) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(action())
        except Exception as error:  # se informa para la aserción
            results.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


class PublicationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu024"))
        self.organizer = Caller(uuid4(), "organizer")

    def draft(self, **overrides: Any) -> UUID:
        return self.db.create_event()(self.organizer, event_command(**overrides)).event.event_id

    def relay(self) -> list[dict[str, Any]]:
        store = SqlAlchemyOutboxStore(self.db.writes)
        claimed = store.claim_batch("w-test", 100, 30)
        for event in claimed:
            store.mark_published(event.event_id, "w-test")
        return [build_envelope(event) for event in claimed]

    def project_all(self, envelopes: list[dict[str, Any]]) -> list[ProjectionOutcome]:
        project = self.db.project()
        return [project(parse_sport_event_message(envelope)) for envelope in envelopes]

    def test_transition_key_and_outbox_are_committed_together(self) -> None:
        """CA1 y CA5: `PUBLISHED`, versión 2, `published_at`, clave y un evento en un COMMIT."""
        event_id = self.draft()
        self.relay()
        key = uuid4()

        result = self.db.publish()(self.organizer, publish_command(event_id, key))

        with self.db.writes() as session:
            row = session.get(EventRow, event_id)
            outbox = session.scalars(
                select(OutboxEventRow).where(OutboxEventRow.event_type == "evento.publicado.v1")
            ).all()
            record = session.get(
                CommandIdempotencyRow, (self.organizer.user_id, "publish_event", key)
            )
        assert row is not None
        assert record is not None
        self.assertEqual((row.status, row.version), ("PUBLISHED", 2))
        self.assertEqual(row.published_at, result.event.published_at)
        self.assertEqual([(m.aggregate_id, m.aggregate_version) for m in outbox], [(event_id, 2)])
        self.assertEqual(outbox[0].causation_id, str(key))
        self.assertEqual(record.response["status"], "PUBLISHED")

    def test_rejections_leave_no_partial_data(self) -> None:
        """CA2 y CA3: `422` o `404` no dejan transición, clave ni evento."""
        event_id = self.draft()
        self.relay()
        with self.db.writes() as session, session.begin():
            # Datos persistidos que ya no cumplen ADR-028 (p. ej., inicio vencido).
            session.execute(
                text(
                    "UPDATE event SET starts_at = now() - interval '1 hour', "
                    "ends_at = now() - interval '1 hour' + make_interval(mins => duration_minutes) "
                    "WHERE event_id = :id"
                ),
                {"id": event_id},
            )

        with self.assertRaises(InvalidSportEvent) as raised:
            self.db.publish()(self.organizer, publish_command(event_id))
        with self.assertRaises(EventNotFound):
            self.db.publish()(Caller(uuid4(), "organizer"), publish_command(event_id))

        self.assertEqual(
            [(v.field, v.issue.value) for v in raised.exception.violations],
            [("starts_at", "not_in_future")],
        )
        with self.db.writes() as session:
            self.assertEqual(session.get(EventRow, event_id).status, "DRAFT")  # type: ignore[union-attr]
        self.assertEqual(self.db.count(CommandIdempotencyRow), 1, "solo la clave de la creación")
        self.assertEqual(self.relay(), [])

    def test_eight_concurrent_publications_produce_one_transition(self) -> None:
        """CA5: ocho publicaciones simultáneas (claves distintas) → una transición y un evento;
        las demás reciben el estado vigente."""
        event_id = self.draft()
        self.relay()

        results = concurrently(
            8, lambda: self.db.publish()(self.organizer, publish_command(event_id))
        )

        self.assertTrue(all(isinstance(r, PublicationResult) for r in results), results)
        self.assertEqual(sum(not r.already_published for r in results), 1)
        self.assertEqual({r.event.version for r in results}, {2})
        self.assertEqual(len(self.relay()), 1)

    def test_eight_concurrent_retries_with_the_same_key(self) -> None:
        """CA4: la misma clave en paralelo → un resultado original y siete repeticiones."""
        event_id = self.draft()
        self.relay()
        key = uuid4()

        results = concurrently(
            8, lambda: self.db.publish()(self.organizer, publish_command(event_id, key))
        )

        self.assertTrue(all(isinstance(r, PublicationResult) for r in results), results)
        self.assertEqual(sum(not r.replayed for r in results), 1)
        self.assertEqual(len({r.event.published_at for r in results}), 1)
        self.assertEqual(len(self.relay()), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.db.publish()(self.organizer, publish_command(self.draft(), key))

    def test_status_and_published_at_must_agree(self) -> None:
        event_id = self.draft()
        for sql in (
            "UPDATE event SET status = 'PUBLISHED' WHERE event_id = :id",
            "UPDATE event SET published_at = now() WHERE event_id = :id",
        ):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError) as raised:
                with self.db.writes() as session, session.begin():
                    session.execute(text(sql), {"id": event_id})
            self.assertIn("ck_event_published_at", str(raised.exception))

    def test_migration_backfills_published_rows_without_date(self) -> None:
        """Decisión del 2026-10-18: un `PUBLISHED` sembrado antes de HU024 recibe
        `published_at = updated_at`; un borrador queda sin fecha."""
        with temporary_database("hu024_backfill") as url:
            upgrade(WRITE_HISTORY, url, revision="0003_event_registration")
            engine = create_engine(url)
            try:
                updated = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
                published_id, draft_id = uuid4(), uuid4()
                with engine.begin() as connection:
                    for event_id, status in ((published_id, "PUBLISHED"), (draft_id, "DRAFT")):
                        connection.execute(
                            text(
                                "INSERT INTO event VALUES (:id, :org, 'Carrera 10K', "
                                "'Descripción suficientemente larga.', 'Running', "
                                "'Parque central', 'Calle 1 # 2-3', 'Bogotá', 'CO', :start, :end, "
                                "'America/Bogota', "
                                "180, 100, :status, 2, :at, :at)"
                            ),
                            {
                                "id": event_id,
                                "org": uuid4(),
                                "start": updated + timedelta(days=30),
                                "end": updated + timedelta(days=30, hours=3),
                                "status": status,
                                "at": updated,
                            },
                        )
                upgrade(WRITE_HISTORY, url)
                with engine.connect() as connection:
                    rows = dict(
                        connection.execute(text("SELECT event_id, published_at FROM event")).all()
                    )
            finally:
                engine.dispose()
        self.assertEqual(rows, {published_id: updated, draft_id: None})

    def test_my_events_keyset_has_no_losses_or_repetitions(self) -> None:
        """CA6: solo los propios, filtro de estado y orden estable con empates de inicio."""
        tied = [self.draft(starts_at="2099-11-15T07:00:00-05:00") for _ in range(4)]
        others = [self.draft(starts_at=f"2099-11-{day:02d}T07:00:00-05:00") for day in (10, 20)]
        foreign = self.db.create_event()(
            Caller(uuid4(), "organizer"), event_command()
        ).event.event_id
        self.project_all(self.relay())
        self.db.publish()(self.organizer, publish_command(others[0]))
        self.project_all(self.relay())
        list_events = self.db.my_events()

        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = list_events(self.organizer, MyEventsFilters(limit=2, cursor=cursor))
            seen.extend(item.event_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        drafts = list_events(self.organizer, MyEventsFilters(status="DRAFT", limit=100))

        self.assertEqual(len(seen), 6)
        self.assertEqual(set(seen), {*tied, *others})
        self.assertNotIn(foreign, seen)
        self.assertEqual(seen[0], others[0])
        self.assertEqual(seen[1:5], sorted(tied))
        self.assertEqual(seen[5], others[1])
        self.assertNotIn(others[0], [item.event_id for item in drafts.items])
        [published] = list_events(self.organizer, MyEventsFilters(status="PUBLISHED")).items
        self.assertEqual(published.event_id, others[0])
        self.assertIsNotNone(published.published_at)

    def test_projection_by_full_state_duplicates_order_and_rebuild(self) -> None:
        """CA1 y CA5: la vista pasa a `PUBLISHED` una vez, una versión menor no retrocede, la
        fila ausente se inserta y la reconstrucción da el mismo resultado; el evento es
        visible en `GET /v1/events` de HU035."""
        event_id = self.draft()
        created = self.relay()
        self.db.publish()(self.organizer, publish_command(event_id))
        published = self.relay()
        envelopes = created + published

        # Fuera de orden y con duplicado: la publicada primero inserta, la creada es menor.
        outcomes = self.project_all([published[0], created[0], published[0]])
        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.STALE, ProjectionOutcome.DUPLICATE],
        )
        snapshot = self.snapshot(event_id)
        self.assertEqual((snapshot["status"], snapshot["version"]), ("PUBLISHED", 2))
        self.assertIsNotNone(snapshot["published_at"])

        # Reconstrucción: vaciar la lectura y reproyectar en orden da lo mismo.
        with self.db.reads() as session, session.begin():
            session.execute(delete(EventReadRow))
            session.execute(delete(ProcessedEventRow))
        self.assertEqual(
            self.project_all(envelopes), [ProjectionOutcome.PROJECTED, ProjectionOutcome.PROJECTED]
        )
        rebuilt = self.snapshot(event_id)
        self.assertEqual(
            {k: v for k, v in rebuilt.items() if k != "projected_at"},
            {k: v for k, v in snapshot.items() if k != "projected_at"},
        )

        now = datetime.now(UTC)
        visible = self.db.published_reader().published(
            PublishedEventsQuery(
                starts_before_or_at=now + timedelta(days=365 * 80),
                ends_at_or_after=now,
                after=None,
                limit=10,
            )
        )
        self.assertEqual([item.event_id for item in visible], [event_id])
        detail = self.db.detail_reader().visible_detail(event_id, athlete_id=None, owner_id=None)
        self.assertIsNotNone(detail)

    def test_concurrent_creation_and_publication_deliveries(self) -> None:
        """Las dos versiones llegan a la vez a una lectura vacía: queda la publicada."""
        event_id = self.draft()
        created = self.relay()
        self.db.publish()(self.organizer, publish_command(event_id))
        published = self.relay()
        pending = iter([parse_sport_event_message(e) for e in created + published] * 2)
        lock = threading.Lock()

        def deliver() -> Any:
            with lock:
                message = next(pending)
            return self.db.project()(message)

        results = concurrently(4, deliver)

        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(self.snapshot(event_id)["status"], "PUBLISHED")
        self.assertEqual(self.db.count(ProcessedEventRow, read=True), 2)

    def test_draft_detail_is_visible_only_to_its_organizer(self) -> None:
        """CA3: el borrador nunca es visible para terceros."""
        event_id = self.draft()
        self.project_all(self.relay())
        reader = self.db.detail_reader()

        own = reader.visible_detail(event_id, athlete_id=None, owner_id=self.organizer.user_id)
        other = reader.visible_detail(event_id, athlete_id=None, owner_id=uuid4())
        athlete = reader.visible_detail(event_id, athlete_id=uuid4(), owner_id=None)

        self.assertEqual(own.event.status if own else None, "DRAFT")
        self.assertEqual((other, athlete), (None, None))

    def snapshot(self, event_id: UUID) -> dict[str, Any]:
        with self.db.reads() as session:
            row = session.get(EventReadRow, event_id)
            assert row is not None
            return {
                column.name: getattr(row, column.name) for column in EventReadRow.__table__.columns
            }


if __name__ == "__main__":
    unittest.main()
