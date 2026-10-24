"""HU022 (ADR-039) sin infraestructura: inscritos de un evento propio con permiso, propiedad,
filtro de estado, keyset, `total`/`by_status`, vacío con total cero, auditoría y API."""

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_create_event import VERIFIER, token

from events_management.api.attendees import build_attendees_router
from events_management.application.errors import PermissionRequired
from events_management.application.ports import AttendeePage, AttendeesQuery, Caller
from events_management.application.queries.attendees import (
    AttendeesFilters,
    InvalidAttendeesQuery,
    ListEventAttendees,
)
from events_management.domain.registrations.model import (
    AppliedFee,
    EventNotAvailable,
    EventRegistration,
    RegistrationStatus,
)
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

BASE = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class AttendeesPermissions:
    """`actor-permissions.v1`: solo `organizer` tiene `events.attendees.view`."""

    def __init__(self, granted: bool = True) -> None:
        self.granted = granted

    def allows(self, actor_type: str, permission: str) -> bool:
        return self.granted and actor_type == "organizer" and permission == "events.attendees.view"


@dataclass
class InMemoryAttendeeReader:
    """Misma semántica que `SqlAlchemyAttendeeReader`."""

    owners: dict[UUID, UUID] = field(default_factory=dict)
    registrations: list[EventRegistration] = field(default_factory=list)
    queries: list[AttendeesQuery] = field(default_factory=list)

    def attendees(self, query: AttendeesQuery) -> AttendeePage | None:
        self.queries.append(query)
        if self.owners.get(query.event_id) != query.organizer_id:
            return None
        own = [r for r in self.registrations if r.event_id == query.event_id]
        by_status = {status.value: 0 for status in RegistrationStatus}
        for item in own:
            by_status[item.status.value] += 1
        rows = sorted(
            (r for r in own if query.status is None or r.status.value == query.status),
            key=lambda r: (r.registered_at, r.registration_id),
        )
        if query.after is not None:
            position = (query.after.registered_at, query.after.registration_id)
            rows = [r for r in rows if (r.registered_at, r.registration_id) > position]
        return AttendeePage(items=tuple(rows[: query.limit]), by_status=by_status)


def registration(event_id: UUID, minutes: int, *, fee: bool = False) -> EventRegistration:
    registered_at = BASE + timedelta(minutes=minutes)
    return EventRegistration(
        registration_id=uuid4(),
        event_id=event_id,
        athlete_id=uuid4(),
        status=RegistrationStatus.REGISTERED,
        registered_at=registered_at,
        applied_fee=AppliedFee(Decimal("35000.00"), "COP") if fee else None,
        version=1,
        created_at=registered_at,
        updated_at=registered_at,
    )


class ListEventAttendeesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.organizer = Caller(user_id=uuid4(), actor_type="organizer")
        self.event_id = uuid4()
        self.reader = InMemoryAttendeeReader(owners={self.event_id: self.organizer.user_id})
        self.list = ListEventAttendees(reader=self.reader, permissions=AttendeesPermissions())

    def test_owner_gets_page_total_and_counts(self) -> None:
        """CA1: lista con estado, fecha y tarifa, más `total` y `by_status`."""
        self.reader.registrations = [registration(self.event_id, m) for m in (3, 1, 2)]

        result = self.list(self.organizer, self.event_id, AttendeesFilters())

        self.assertEqual(
            [item.registered_at for item in result.page.items],
            sorted(item.registered_at for item in result.page.items),
        )
        self.assertEqual((result.total, result.page.by_status), (3, {"REGISTERED": 3}))
        self.assertIsNone(result.next_cursor)
        self.assertEqual(self.reader.queries[0].status, "REGISTERED", "por defecto REGISTERED")

    def test_all_status_has_no_filter(self) -> None:
        self.reader.registrations = [registration(self.event_id, 1)]

        result = self.list(self.organizer, self.event_id, AttendeesFilters(status="ALL"))

        self.assertIsNone(self.reader.queries[0].status)
        self.assertEqual(result.total, 1)

    def test_keyset_pages_are_stable_and_complete(self) -> None:
        """CA4: orden `registered_at`, `registration_id` ascendente sin pérdidas ni repeticiones;
        empates de `registered_at` resueltos por `registration_id`."""
        same = BASE + timedelta(minutes=10)
        tied = [registration(self.event_id, 10) for _ in range(3)]
        for item in tied:
            self.assertEqual(item.registered_at, same)
        self.reader.registrations = [registration(self.event_id, m) for m in range(7)] + tied

        seen: list[UUID] = []
        cursor = None
        while True:
            result = self.list(
                self.organizer, self.event_id, AttendeesFilters(limit=3, cursor=cursor)
            )
            seen.extend(item.registration_id for item in result.page.items)
            self.assertEqual(result.total, 10)
            cursor = result.next_cursor
            if cursor is None:
                break

        expected = sorted(
            self.reader.registrations, key=lambda r: (r.registered_at, r.registration_id)
        )
        self.assertEqual(seen, [item.registration_id for item in expected])

    def test_no_attendees_is_empty_with_total_zero(self) -> None:
        """CA3."""
        result = self.list(self.organizer, self.event_id, AttendeesFilters(status="ALL"))

        self.assertEqual((result.page.items, result.total, result.next_cursor), ((), 0, None))
        self.assertEqual(result.page.by_status, {"REGISTERED": 0})

    def test_foreign_or_missing_event_is_not_found(self) -> None:
        """CA2: sin revelar participantes."""
        other = Caller(user_id=uuid4(), actor_type="organizer")
        self.reader.registrations = [registration(self.event_id, 1)]

        for caller, event_id in ((other, self.event_id), (self.organizer, uuid4())):
            with self.subTest(event=str(event_id)), self.assertRaises(EventNotAvailable):
                self.list(caller, event_id, AttendeesFilters())

    def test_only_organizers_with_the_permission(self) -> None:
        for caller, permissions in (
            (Caller(uuid4(), "athlete"), AttendeesPermissions()),
            (Caller(uuid4(), "service_provider"), AttendeesPermissions()),
            (self.organizer, AttendeesPermissions(granted=False)),
        ):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                ListEventAttendees(reader=self.reader, permissions=permissions)(
                    caller, self.event_id, AttendeesFilters()
                )
        self.assertEqual(self.reader.queries, [])

    def test_invalid_filters(self) -> None:
        """CA4: estado desconocido, límite fuera de rango y cursor inválido → `422`."""
        for filters, expected in (
            (AttendeesFilters(status="CANCELLED"), ("status", "unknown_value")),
            (AttendeesFilters(limit=0), ("limit", "out_of_range")),
            (AttendeesFilters(limit=101), ("limit", "out_of_range")),
            (AttendeesFilters(cursor="no-es-cursor"), ("cursor", "invalid_cursor")),
        ):
            with self.subTest(filters=filters), self.assertRaises(InvalidAttendeesQuery) as raised:
                self.list(self.organizer, self.event_id, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), expected)

    def test_audit_records_access_without_attendee_data(self) -> None:
        item = registration(self.event_id, 1)
        self.reader.registrations = [item]

        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            self.list(self.organizer, self.event_id, AttendeesFilters())
            with self.assertRaises(EventNotAvailable):
                self.list(Caller(uuid4(), "organizer"), self.event_id, AttendeesFilters())

        fields = [r.fields for r in audit.records if r.getMessage() == "audit.attendees_access"]  # type: ignore[attr-defined]
        self.assertEqual([entry["result"] for entry in fields], ["listed", "rejected"])
        self.assertEqual(fields[0]["returned"], 1)
        self.assertEqual(fields[1]["reason"], "event_not_found")
        self.assertNotIn(str(item.athlete_id), " ".join(audit.output) + str(fields))


class AttendeesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.organizer_id, self.event_id = uuid4(), uuid4()
        self.reader = InMemoryAttendeeReader(owners={self.event_id: self.organizer_id})
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_attendees_router(
                    verify_token=VERIFIER.verify,
                    list_attendees=ListEventAttendees(
                        reader=self.reader, permissions=AttendeesPermissions()
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.organizer_id, 'organizer')}"}

    def get(self, event_id: UUID | str | None = None, **params: str) -> Any:
        return self.client.get(
            f"/v1/me/events/{event_id or self.event_id}/registrations",
            params=params,
            headers=self.auth,
        )

    def test_response_shape_and_no_store(self) -> None:
        item = registration(self.event_id, 1, fee=True)
        self.reader.registrations = [item]

        response = self.get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "items": [
                    {
                        "registration_id": str(item.registration_id),
                        "athlete_id": str(item.athlete_id),
                        "status": "REGISTERED",
                        "registered_at": "2026-10-20T12:01:00Z",
                        "applied_fee": {"amount": "35000.00", "currency": "COP"},
                    }
                ],
                "total": 1,
                "by_status": {"REGISTERED": 1},
                "next_cursor": None,
            },
        )
        self.assertNotIn("email", response.text)

    def test_errors(self) -> None:
        self.assertEqual(
            (self.get(uuid4()).status_code, self.get(uuid4()).json()["code"]),
            (404, "event_not_found"),
        )
        invalid = self.get(status="X", limit="abc")
        self.assertEqual(
            (invalid.status_code, invalid.json()["details"][0]["field"]), (422, "query.status")
        )
        self.assertEqual(
            self.get(limit="abc").json()["details"],
            [{"field": "query.limit", "issue": "out_of_range"}],
        )
        athlete = self.client.get(
            f"/v1/me/events/{self.event_id}/registrations",
            headers={"Authorization": f"Bearer {token(uuid4(), 'athlete')}"},
        )
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            self.client.get(f"/v1/me/events/{self.event_id}/registrations").status_code, 401
        )
        self.assertEqual(self.get("no-es-uuid").status_code, 422)


if __name__ == "__main__":
    unittest.main()
