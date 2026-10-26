"""HU025 sin infraestructura: reglas de la tarifa, comando, idempotencia, versión, proyección
y API HTTP (CA1 a CA5, ADR-040)."""

import logging
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, InMemoryStore, ReadStore
from fastapi.testclient import TestClient
from publication_fakes import InMemoryPublicationUnitOfWork, draft
from test_create_event import VERIFIER, token

from events_management.api.fee import build_fee_router, parse_fee
from events_management.application.commands.event_fee import (
    ChangeEventFee,
    ChangeEventFeeCommand,
    FeeChangeResult,
)
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.application.messages import (
    EventFeeUpdatedMessage,
    parse_event_aggregate_document,
)
from events_management.application.ports import Caller
from events_management.application.projections.event_projection import (
    EventsProjector,
    ProjectionOutcome,
)
from events_management.domain.sport_events.fee import (
    EventFee,
    FeeChange,
    FeeInput,
    VersionConflict,
    validate_fee,
    validate_removal,
)
from events_management.domain.sport_events.model import EventNotFound, EventStatus, SportEvent
from events_management.domain.validation import InvalidSportEvent
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

COP = "COP"


class PricingPermissions:
    """`actor-permissions.v1`: solo `organizer` tiene `events.pricing.manage`."""

    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "events.pricing.manage"


class OnlyPublish:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "events.publish"


class InMemoryFeeUnitOfWork(InMemoryPublicationUnitOfWork):
    """La transacción de HU025: misma confirmación diferida que la publicación."""

    def __init__(self, store: InMemoryStore, registrations: dict[UUID, int], **kwargs: Any) -> None:
        super().__init__(store, **kwargs)
        self.registrations = registrations

    def update_fee(self, event: SportEvent) -> None:
        self.update_event(event)

    def count_registrations(self, event_id: UUID) -> int:
        return self.registrations.get(event_id, 0)


def fee_input(**changes: Any) -> FeeInput:
    values: dict[str, Any] = {"amount": "85000.00", "currency": COP, "expected_version": 1}
    values.update(changes)
    return parse_fee(values)


def issues(error: InvalidSportEvent) -> list[tuple[str | None, str]]:
    return [(v.field, v.issue.value) for v in error.violations]


class FeeRulesTest(unittest.TestCase):
    def test_valid_amounts_including_free(self) -> None:
        """CA1: dos decimales entre `0.00` (gratuito) y `99999999.99`, solo `COP`."""
        for amount in ("0.00", "1.50", "85000.00", "99999999.99"):
            with self.subTest(amount=amount):
                change = validate_fee(fee_input(amount=amount))
                self.assertEqual(change.fee, EventFee(Decimal(amount), COP))
                self.assertEqual(change.expected_version, 1)

    def test_invalid_amounts_currency_and_version(self) -> None:
        """CA2: negativo, más de dos decimales, fuera de rango, otra moneda → `422`."""
        cases: dict[str, tuple[dict[str, Any], list[tuple[str | None, str]]]] = {
            "negativo": ({"amount": "-1.00"}, [("amount", "out_of_range")]),
            "tres decimales": ({"amount": "10.001"}, [("amount", "too_many_decimals")]),
            "un decimal": ({"amount": "10.5"}, [("amount", "unknown_value")]),
            "entero": ({"amount": "10"}, [("amount", "unknown_value")]),
            "exponente": ({"amount": "1E+3"}, [("amount", "unknown_value")]),
            "fuera de rango": ({"amount": "100000000.00"}, [("amount", "out_of_range")]),
            "número JSON": ({"amount": 10.5}, [("amount", "unknown_value")]),
            "otra moneda": ({"currency": "USD"}, [("currency", "unsupported_currency")]),
            "versión cero": ({"expected_version": 0}, [("expected_version", "out_of_range")]),
            "versión texto": ({"expected_version": "1"}, [("expected_version", "unknown_value")]),
            "versión booleana": (
                {"expected_version": True},
                [("expected_version", "unknown_value")],
            ),
            "faltantes": (
                {"amount": None, "currency": None, "expected_version": None},
                [
                    ("amount", "required"),
                    ("currency", "required"),
                    ("expected_version", "required"),
                ],
            ),
        }
        for name, (changes, expected) in cases.items():
            with self.subTest(case=name), self.assertRaises(InvalidSportEvent) as raised:
                validate_fee(fee_input(**changes))
            self.assertEqual(sorted(issues(raised.exception)), sorted(expected))

    def test_extra_fields_and_non_object_bodies(self) -> None:
        for body, expected in (
            (
                {"amount": "1.00", "currency": COP, "expected_version": 1, "status": "PAID"},
                [("status", "field_not_allowed")],
            ),
            (None, [(None, "required")]),
            ([1], [(None, "unknown_value")]),
        ):
            with self.subTest(body=body), self.assertRaises(InvalidSportEvent) as raised:
                validate_fee(parse_fee(body))
            self.assertEqual(issues(raised.exception), expected)

    def test_removal_only_needs_a_valid_version(self) -> None:
        self.assertEqual(validate_removal(4, []), FeeChange(None, 4))
        with self.assertRaises(InvalidSportEvent):
            validate_removal(0, [])

    def test_change_fee_on_the_aggregate(self) -> None:
        """CA1 y CA3: una subida de versión con la versión vigente; obsoleta → conflicto;
        iniciado → `event_already_started`. `DRAFT` y `PUBLISHED` se admiten."""
        event = draft(uuid4())
        at = NOW + timedelta(hours=1)
        fee = EventFee(Decimal("10.00"), COP)

        changed = event.change_fee(FeeChange(fee, 1), at)
        published = event.publish(at).change_fee(FeeChange(fee, 2), at)

        self.assertEqual((changed.fee, changed.version, changed.fee_updated_at), (fee, 2, at))
        self.assertEqual(changed.updated_at, at)
        self.assertEqual((published.status, published.version), (EventStatus.PUBLISHED, 3))
        self.assertEqual(changed.change_fee(FeeChange(None, 2), at).fee, None)
        with self.assertRaises(VersionConflict) as conflict:
            changed.change_fee(FeeChange(fee, 1), at)
        self.assertEqual(conflict.exception.current_version, 2)
        with self.assertRaises(InvalidSportEvent) as started:
            event.change_fee(FeeChange(fee, 1), event.details.starts_at)
        self.assertEqual(issues(started.exception), [("starts_at", "event_already_started")])


class ChangeEventFeeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.registrations: dict[UUID, int] = {}
        self.clock = FixedClock()
        self.organizer = Caller(uuid4(), "organizer")
        self.event = draft(self.organizer.user_id)
        self.store.events[self.event.event_id] = self.event
        self.change = self.use_case()

    def use_case(
        self, permissions: Any = None, before_commit: list[Any] | None = None
    ) -> ChangeEventFee:
        hooks = before_commit if before_commit is not None else []

        def unit_of_work() -> InMemoryFeeUnitOfWork:
            hook = hooks.pop(0) if hooks else None
            return InMemoryFeeUnitOfWork(self.store, self.registrations, before_commit=hook)

        return ChangeEventFee(
            unit_of_work=unit_of_work,
            permissions=permissions or PricingPermissions(),
            clock=self.clock,
        )

    def command(
        self,
        amount: str | None = "85000.00",
        version: int = 1,
        key: UUID | None = None,
        event_id: UUID | None = None,
    ) -> ChangeEventFeeCommand:
        fee = EventFee(Decimal(amount), COP) if amount is not None else None
        return ChangeEventFeeCommand(
            event_id=event_id or self.event.event_id,
            change=FeeChange(fee, version),
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
        )

    def test_assigns_with_one_version_one_event_and_registration_count(self) -> None:
        """CA1 y CA4: `200` con la tarifa, versión + 1, `registration_count` y un evento."""
        self.registrations[self.event.event_id] = 3
        key = uuid4()

        with self.assertLogs("sportapp.audit", level=logging.INFO) as logs:
            result = self.change(self.organizer, self.command(key=key))

        stored = self.store.events[self.event.event_id]
        self.assertEqual((stored.version, stored.fee), (2, EventFee(Decimal("85000.00"), COP)))
        self.assertEqual(result.registration_count, 3)
        self.assertEqual(result.document()["fee"], {"amount": "85000.00", "currency": COP})
        self.assertEqual(result.document()["registration_count"], 3)
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "evento.tarifa_actualizada.v1")
        self.assertEqual((message.aggregate_id, message.aggregate_version), (stored.event_id, 2))
        self.assertEqual(message.causation_id, str(key))
        # Estado completo del evento, sin conteos ni datos de pago (ADR-040).
        self.assertEqual(message.payload["fee"], {"amount": "85000.00", "currency": COP})
        self.assertNotIn("registration_count", message.payload)
        self.assertNotIn("payment_status", message.payload)
        self.assertEqual(parse_event_aggregate_document(message.payload), stored)
        audit = logs.records[0].fields  # type: ignore[attr-defined]
        self.assertEqual((audit["result"], audit["operation"]), ("changed", "put_event_fee"))
        self.assertNotIn("85000", str(audit))

    def test_free_event_and_removal(self) -> None:
        """`0.00` es gratuito; `DELETE` deja `fee = null` con otra versión y otro evento."""
        free = self.change(self.organizer, self.command(amount="0.00"))
        removed = self.change(self.organizer, self.command(amount=None, version=2))

        self.assertEqual(free.document()["fee"], {"amount": "0.00", "currency": COP})
        self.assertIsNone(removed.event.fee)
        self.assertIsNotNone(removed.event.fee_updated_at)
        self.assertEqual(removed.event.version, 3)
        self.assertEqual([m.payload["fee"] for m in self.store.outbox][1], None)

    def test_same_key_returns_the_original_response(self) -> None:
        """CA5: misma clave y contenido → la respuesta original, sin otra versión ni evento."""
        key = uuid4()
        first = self.change(self.organizer, self.command(key=key))
        self.registrations[self.event.event_id] = 9

        again = self.change(self.organizer, self.command(key=key))

        self.assertTrue(again.replayed)
        self.assertEqual(again.document(), first.document())
        self.assertEqual(len(self.store.outbox), 1)

    def test_same_key_with_another_payload_is_reused(self) -> None:
        key = uuid4()
        self.change(self.organizer, self.command(key=key))

        for other in (
            self.command(amount="1.00", key=key),
            self.command(version=2, key=key),
        ):
            with self.subTest(command=other), self.assertRaises(IdempotencyKeyReused):
                self.change(self.organizer, other)
        self.assertEqual(len(self.store.outbox), 1)

    def test_put_and_delete_keys_are_separate_operations(self) -> None:
        key = uuid4()
        self.change(self.organizer, self.command(key=key))

        removed = self.change(self.organizer, self.command(amount=None, version=2, key=key))

        self.assertFalse(removed.replayed)
        self.assertIsNone(removed.event.fee)

    def test_stale_version_is_a_conflict_and_nothing_changes(self) -> None:
        """CA3: versión obsoleta → `version_conflict` con la vigente."""
        self.change(self.organizer, self.command())

        with self.assertRaises(VersionConflict) as raised:
            self.change(self.organizer, self.command(amount="1.00", version=1))

        self.assertEqual(raised.exception.current_version, 2)
        self.assertEqual(self.store.events[self.event.event_id].fee.amount, Decimal("85000.00"))  # type: ignore[union-attr]
        self.assertEqual(len(self.store.outbox), 1)

    def test_started_event_is_rejected(self) -> None:
        """CA2: después de `starts_at` → `422 event_already_started`, sin cambios."""
        self.clock.current = self.event.details.starts_at

        with self.assertRaises(InvalidSportEvent) as raised:
            self.change(self.organizer, self.command())

        self.assertEqual(issues(raised.exception), [("starts_at", "event_already_started")])
        self.assertEqual((self.store.outbox, self.store.idempotency), ([], {}))

    def test_foreign_or_missing_event_is_not_found(self) -> None:
        """CA3: ajeno o inexistente → `event_not_found`, sin clave ni evento."""
        foreign = draft(uuid4())
        self.store.events[foreign.event_id] = foreign
        for event_id in (foreign.event_id, uuid4()):
            with self.subTest(event_id=event_id), self.assertRaises(EventNotFound):
                self.change(self.organizer, self.command(event_id=event_id))
        self.assertEqual((self.store.outbox, self.store.idempotency), ([], {}))

    def test_only_organizer_with_pricing_permission(self) -> None:
        for caller, permissions in (
            (Caller(uuid4(), "athlete"), PricingPermissions()),
            (Caller(uuid4(), "service_provider"), PricingPermissions()),
            (self.organizer, OnlyPublish()),
        ):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.use_case(permissions)(caller, self.command())
        self.assertEqual(self.store.outbox, [])

    def test_concurrent_same_key_returns_the_first_result(self) -> None:
        key = uuid4()
        rival = self.use_case()
        racing = self.use_case(
            before_commit=[None, lambda: rival(self.organizer, self.command(key=key))]
        )

        result = racing(self.organizer, self.command(key=key))

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(self.store.events[self.event.event_id].version, 2)

    def test_expired_key_is_a_new_request(self) -> None:
        key = uuid4()
        self.change(self.organizer, self.command(key=key))
        self.clock.advance(timedelta(hours=24, seconds=1))

        result = self.change(self.organizer, self.command(amount="1.00", version=2, key=key))

        self.assertFalse(result.replayed)
        self.assertEqual(result.event.version, 3)


class FeeProjectionTest(unittest.TestCase):
    """CMP-004 / ADR-033: la tarifa se proyecta por estado completo y versión mayor."""

    def setUp(self) -> None:
        self.store = ReadStore()
        self.project = EventsProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
        )
        self.event = draft(uuid4())

    def message(self, event: SportEvent) -> EventFeeUpdatedMessage:
        return EventFeeUpdatedMessage(uuid4(), event.version, None, event)

    def test_greater_version_replaces_and_lower_is_stale(self) -> None:
        fee = EventFee(Decimal("10.00"), COP)
        v2 = self.event.change_fee(FeeChange(fee, 1), NOW)
        v3 = v2.change_fee(FeeChange(None, 2), NOW)

        outcomes = [self.project(self.message(e)) for e in (v3, v2)]

        self.assertEqual(outcomes, [ProjectionOutcome.PROJECTED, ProjectionOutcome.STALE])
        self.assertEqual(self.store.views[self.event.event_id].version, 3)
        self.assertIsNone(self.store.views[self.event.event_id].fee)

    def test_duplicate_delivery_is_ignored(self) -> None:
        message = self.message(self.event.change_fee(FeeChange(None, 1), NOW))

        self.assertEqual(self.project(message), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(message), ProjectionOutcome.DUPLICATE)


class FeeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.registrations: dict[UUID, int] = {}
        self.clock = FixedClock()
        self.organizer_id = uuid4()
        self.event = draft(self.organizer_id)
        self.store.events[self.event.event_id] = self.event
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_fee_router(
                    verify_token=VERIFIER.verify,
                    change_fee=ChangeEventFee(
                        unit_of_work=lambda: InMemoryFeeUnitOfWork(self.store, self.registrations),
                        permissions=PricingPermissions(),
                        clock=self.clock,
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def headers(self, actor: str = "organizer", key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token(self.organizer_id, actor)}"}
        return headers | ({"Idempotency-Key": key} if key else {})

    def put(self, body: Any, key: str | None = None, actor: str = "organizer") -> Any:
        return self.client.put(
            f"/v1/events/{self.event.event_id}/fee",
            json=body,
            headers=self.headers(actor, key or str(uuid4())),
        )

    def body(self, **changes: Any) -> dict[str, Any]:
        return {"amount": "85000.00", "currency": COP, "expected_version": 1, **changes}

    def test_put_returns_the_owned_aggregate_and_replays(self) -> None:
        """CA1, CA4 y CA5 por HTTP."""
        self.registrations[self.event.event_id] = 2
        key = str(uuid4())

        response = self.put(self.body(), key)
        replay = self.put(self.body(), key)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["fee"], {"amount": "85000.00", "currency": COP})
        self.assertEqual((body["version"], body["registration_count"]), (2, 2))
        self.assertEqual(body["fee_updated_at"], "2026-10-12T21:00:00Z")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual((replay.json(), replay.headers["Idempotent-Replayed"]), (body, "true"))

    def test_delete_removes_the_fee(self) -> None:
        self.put(self.body())

        response = self.client.delete(
            f"/v1/events/{self.event.event_id}/fee",
            params={"expected_version": "2"},
            headers=self.headers(key=str(uuid4())),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["fee"], response.json()["version"]), (None, 3))

    def test_errors(self) -> None:
        """CA2 y CA3 por HTTP: `422` con `details`, `409` y `404`."""
        reused = str(uuid4())
        self.put(self.body(), reused)
        cases: dict[str, tuple[Any, int, str, list[dict[str, Any]] | None]] = {
            "moneda": (
                self.put(self.body(currency="USD", expected_version=2)),
                422,
                "validation_error",
                [{"field": "currency", "issue": "unsupported_currency"}],
            ),
            "decimales": (
                self.put(self.body(amount="1.234", expected_version=2)),
                422,
                "validation_error",
                [{"field": "amount", "issue": "too_many_decimals"}],
            ),
            "negativo": (
                self.put(self.body(amount="-5.00", expected_version=2)),
                422,
                "validation_error",
                [{"field": "amount", "issue": "out_of_range"}],
            ),
            "versión": (
                self.put(self.body(expected_version=1)),
                409,
                "version_conflict",
                [{"field": "expected_version", "issue": "2"}],
            ),
            "clave": (
                self.put(self.body(amount="1.00", expected_version=2), reused),
                409,
                "idempotency_key_reused",
                None,
            ),
            "sin clave": (
                self.client.put(
                    f"/v1/events/{self.event.event_id}/fee",
                    json=self.body(amount="1.234"),
                    headers=self.headers(),
                ),
                422,
                "validation_error",
                [
                    {"field": "Idempotency-Key", "issue": "required"},
                    {"field": "amount", "issue": "too_many_decimals"},
                ],
            ),
            "ajeno": (
                self.client.put(
                    f"/v1/events/{uuid4()}/fee",
                    json=self.body(),
                    headers=self.headers(key=str(uuid4())),
                ),
                404,
                "event_not_found",
                None,
            ),
            "actor": (
                self.put(self.body(amount="bad"), actor="athlete"),
                403,
                "permission_required",
                None,
            ),
        }
        for name, (response, status, code, details) in cases.items():
            with self.subTest(case=name):
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                if details is not None:
                    self.assertEqual(response.json()["details"], details)
        self.assertEqual(len(self.store.outbox), 1)

    def test_started_event_and_delete_query_errors(self) -> None:
        url = f"/v1/events/{self.event.event_id}/fee"
        for raw, issue in (
            (None, "required"),
            ("abc", "unknown_value"),
            ("-1", "unknown_value"),
            ("0", "out_of_range"),
        ):
            params = {} if raw is None else {"expected_version": raw}
            with self.subTest(expected_version=raw):
                response = self.client.delete(
                    url, params=params, headers=self.headers(key=str(uuid4()))
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["details"],
                    [{"field": "query.expected_version", "issue": issue}],
                )
        self.clock.current = self.event.details.starts_at + timedelta(minutes=1)
        late = self.put(self.body())
        self.assertEqual(late.status_code, 422)
        self.assertEqual(
            late.json()["details"], [{"field": "starts_at", "issue": "event_already_started"}]
        )


class FeeResultTest(unittest.TestCase):
    def test_document_round_trip(self) -> None:
        event = draft(uuid4()).change_fee(FeeChange(EventFee(Decimal("5.00"), COP), 1), NOW)
        result = FeeChangeResult(event, 4)

        parsed = parse_event_aggregate_document(result.document())

        self.assertEqual(parsed, replace(event))


if __name__ == "__main__":
    unittest.main()
