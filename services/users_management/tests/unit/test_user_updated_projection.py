"""HU002 (ADR-035): lectura validada de `usuario.actualizado.v1` y su proyección en
`user_directory` sin infraestructura (CMP-001)."""

import unittest
from dataclasses import replace
from uuid import UUID, uuid4

from projection_fakes import (
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    registered_envelope,
    updated_envelope,
)

from users_management.application.messages import (
    InvalidEvent,
    UserRegisteredEvent,
    UserUpdatedEvent,
    parse_users_event,
)
from users_management.application.projections.users_projection import (
    DIRECTORY_PROJECTION,
    DirectoryEmailInUse,
    ProjectionGap,
    ProjectionOutcome,
    UsersProjector,
)


def updated_event(user_id: UUID, version: int = 2, **fields: object) -> UserUpdatedEvent:
    event = parse_users_event(updated_envelope(user_id, version, **fields))
    assert isinstance(event, UserUpdatedEvent)
    return event


class ParseUserUpdatedTest(unittest.TestCase):
    def test_valid_update(self) -> None:
        user_id = uuid4()

        event = updated_event(user_id, 3, email="ana@example.com", status="disabled")

        self.assertEqual((event.user_id, event.aggregate_version), (user_id, 3))
        self.assertEqual((event.email, event.status), ("ana@example.com", "disabled"))
        self.assertNotIn("ana@example.com", repr(event))

    def test_contract_violations(self) -> None:
        user_id = uuid4()
        valid = updated_envelope(user_id)
        cases = {
            "aggregate_version_invalid": updated_envelope(user_id, 1),
            "version_mismatch": {**valid, "payload": {**valid["payload"], "version": 3}},
            "aggregate_id_mismatch": {
                **valid,
                "payload": {**valid["payload"], "user_id": str(uuid4())},
            },
            "payload_fields_mismatch": {
                **valid,
                "payload": {**valid["payload"], "password_hash": "x"},
            },
            "full_name_invalid": updated_envelope(user_id, full_name=""),
            "email_invalid": updated_envelope(user_id, email="a"),
            "unknown_actor_type": updated_envelope(user_id, actor_type="admin"),
            "unknown_status": updated_envelope(user_id, status="deleted"),
            "updated_at_invalid": updated_envelope(user_id, updated_at="2026-10-24T12:00:00+05:00"),
            "unsupported_schema_version": {**valid, "schema_version": 2},
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_users_event(envelope)
            self.assertEqual(raised.exception.reason, reason)


class UserUpdatedProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.project = UsersProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )
        registered = parse_users_event(registered_envelope(email="ana@example.com"))
        assert isinstance(registered, UserRegisteredEvent)
        self.project(registered)
        self.user_id = registered.user_id

    def test_update_replaces_name_and_email_with_the_next_version(self) -> None:
        """CA1: el directorio refleja el cambio una vez proyectado."""
        event = updated_event(self.user_id, full_name="Ana María", email="nuevo@example.com")

        first = self.project(event)
        again = self.project(event)

        self.assertEqual((first, again), (ProjectionOutcome.APPLIED, ProjectionOutcome.DUPLICATE))
        entry = self.store.directory[self.user_id]
        self.assertEqual(
            (entry.full_name, entry.email, entry.aggregate_version),
            ("Ana María", "nuevo@example.com", 2),
        )
        self.assertEqual(self.store.processed[event.event_id][0], DIRECTORY_PROJECTION)

    def test_consecutive_updates_end_in_the_highest_version(self) -> None:
        self.project(updated_event(self.user_id, 2, full_name="Dos"))
        self.project(updated_event(self.user_id, 3, full_name="Tres"))

        outcome = self.project(updated_event(self.user_id, 2, full_name="Vieja"))

        self.assertEqual(outcome, ProjectionOutcome.OBSOLETE)
        entry = self.store.directory[self.user_id]
        self.assertEqual((entry.full_name, entry.aggregate_version), ("Tres", 3))

    def test_gap_fails_without_effects(self) -> None:
        """Falta la versión 2: no se adivina el estado intermedio (CMP-001)."""
        before = dict(self.store.directory)
        processed = dict(self.store.processed)

        with self.assertRaises(ProjectionGap):
            self.project(updated_event(self.user_id, 3))

        self.assertEqual((self.store.directory, self.store.processed), (before, processed))

    def test_update_before_registration_is_a_gap(self) -> None:
        with self.assertRaises(ProjectionGap):
            self.project(updated_event(uuid4()))

    def test_email_still_held_by_another_row_is_retried(self) -> None:
        """Otra cuenta liberó el correo pero su edición aún no se proyecta: se reintenta."""
        other = parse_users_event(registered_envelope(email="ocupado@example.com"))
        assert isinstance(other, UserRegisteredEvent)
        self.project(other)
        event = updated_event(self.user_id, email="ocupado@example.com")

        with self.assertRaises(DirectoryEmailInUse):
            self.project(event)
        self.assertNotIn(event.event_id, self.store.processed)

        self.project(updated_event(other.user_id, email="liberado@example.com"))
        self.assertEqual(self.project(event), ProjectionOutcome.APPLIED)
        self.assertEqual(self.store.directory[self.user_id].email, "ocupado@example.com")

    def test_redelivery_with_another_event_id_is_obsolete(self) -> None:
        event = updated_event(self.user_id)
        self.project(event)

        self.assertEqual(self.project(replace(event, event_id=uuid4())), ProjectionOutcome.OBSOLETE)
