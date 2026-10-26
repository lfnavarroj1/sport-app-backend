"""HU018 (T-HU018-BE-EVT): `GET /v1/events` contra el contrato `actor-permissions.v2` real.

El servicio admite al `service_provider` por `calendar.integrated.view` (ADR-039), el permiso
que ese contrato ya le concede; el organizador sigue entrando por `events.calendar.view`.
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from events_management.application.errors import PermissionRequired
from events_management.application.ports import (
    CalendarEventView,
    Caller,
    PublishedEventsQuery,
)
from events_management.application.queries.published_events import (
    ORGANIZER_CALENDAR_PERMISSION,
    PROVIDER_CALENDAR_PERMISSION,
    EventsFilters,
    ListPublishedEvents,
)
from events_management.infrastructure.authorization import ContractPermissionPolicy
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path

FROM = datetime(2026, 11, 1, tzinfo=UTC)
TO = FROM + timedelta(days=30)


class NoAffiliation:
    def has_capability(self, user_id: UUID, capability: str) -> bool:
        raise AssertionError("el profesional y el organizador no consultan Afiliación")


class EmptyReader:
    def published(self, query: PublishedEventsQuery) -> list[CalendarEventView]:
        return []


class ProviderPermissionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ContractPermissionPolicy(
            load_actor_permissions(resolve_actor_permissions_path())
        )
        self.list = ListPublishedEvents(
            reader=EmptyReader(), capabilities=NoAffiliation(), permissions=self.policy
        )

    def test_contract_grants_the_permissions_the_query_requires(self) -> None:
        self.assertEqual(PROVIDER_CALENDAR_PERMISSION, "calendar.integrated.view")
        self.assertTrue(self.policy.allows("service_provider", PROVIDER_CALENDAR_PERMISSION))
        self.assertFalse(self.policy.allows("service_provider", ORGANIZER_CALENDAR_PERMISSION))
        self.assertTrue(self.policy.allows("organizer", ORGANIZER_CALENDAR_PERMISSION))

    def test_provider_and_organizer_are_admitted_with_the_real_contract(self) -> None:
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor):
                page = self.list(Caller(uuid4(), actor), EventsFilters(FROM, TO))
                self.assertEqual(page.items, ())

    def test_unknown_actor_is_still_rejected(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "administrator"), EventsFilters(FROM, TO))


if __name__ == "__main__":
    unittest.main()
