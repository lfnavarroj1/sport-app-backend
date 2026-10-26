"""Componente HU025 en web: BFF → Usuarios (sesión y JWKS) → Eventos → outbox → Pub/Sub →
`eventos-read-projector` → `event_read.fee_*`, visible en el detalle público y en «mis
eventos» con `registration_count`; las inscripciones previas conservan su `applied_fee`.

Reutiliza `EventFlowCase` de HU024 (servicios reales con PostgreSQL migrado y el emulador
de Pub/Sub con un proyecto propio). Las inscripciones (HU041) se hacen contra el servicio,
porque el canal web no las expone.
"""

import os
import unittest
import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select, text
from test_event_publication_flow import EventFlowCase

from events_management.infrastructure.persistence_read.models import EventReadRow
from events_management.infrastructure.persistence_write.models import (
    EventRegistrationRow,
    OutboxEventRow,
)
from events_management.workers.pubsub_setup import ensure_resources

FEE = {"amount": "85000.00", "currency": "COP"}


class EventFeeFlowTest(EventFlowCase):
    def put_fee(
        self, user: dict[str, str], event_id: str, body: dict[str, Any], key: str | None = None
    ) -> httpx.Response:
        headers = {**user, "Idempotency-Key": key or str(uuid.uuid4())}
        return self.bff.put(f"/v1/events/{event_id}/fee", json=body, headers=headers)

    def register(self, athlete: dict[str, str], event_id: str) -> dict[str, Any]:
        response = self.events.post(
            f"/v1/events/{event_id}/registrations",
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body: dict[str, Any] = response.json()
        return body

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_fee_flow_through_the_web_channel(self) -> None:
        """CA1, CA3, CA4 y CA5 de punta a punta en el backend."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        organizer, stranger = self.session_for("organizer"), self.session_for("organizer")
        athlete = self.session_for("athlete")
        event_id = self.draft(organizer)
        self.assertEqual(self.publish(organizer, event_id, str(uuid.uuid4())).status_code, 200)
        earlier = self.register(athlete, event_id)

        # CA1, CA4 y CA5: asignar con la versión vigente, repetir la clave.
        key = str(uuid.uuid4())
        assigned = self.put_fee(organizer, event_id, {**FEE, "expected_version": 2}, key)
        replay = self.put_fee(organizer, event_id, {**FEE, "expected_version": 2}, key)
        self.assertEqual(assigned.status_code, 200, assigned.text)
        body = assigned.json()
        self.assertEqual((body["fee"], body["version"]), (FEE, 3))
        self.assertEqual(body["registration_count"], 1)
        self.assertEqual((replay.json(), replay.headers["Idempotent-Replayed"]), (body, "true"))
        later = self.register(self.session_for("athlete"), event_id)

        # CA2 y CA3: moneda, decimales, versión obsoleta y evento ajeno, sin cambios.
        currency = self.put_fee(
            organizer, event_id, {**FEE, "currency": "USD", "expected_version": 3}
        )
        decimals = self.put_fee(
            organizer, event_id, {"amount": "1.234", "currency": "COP", "expected_version": 3}
        )
        stale = self.put_fee(organizer, event_id, {**FEE, "expected_version": 2})
        foreign = self.put_fee(stranger, event_id, {**FEE, "expected_version": 3})
        self.assertEqual(
            (currency.status_code, currency.json()["details"]),
            (422, [{"field": "currency", "issue": "unsupported_currency"}]),
        )
        self.assertEqual(
            decimals.json()["details"], [{"field": "amount", "issue": "too_many_decimals"}]
        )
        self.assertEqual(
            (stale.status_code, stale.json()["code"], stale.json()["details"]),
            (409, "version_conflict", [{"field": "expected_version", "issue": "3"}]),
        )
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "event_not_found"))

        # CA4: la inscripción previa conserva su `applied_fee`; la nueva toma la vigente.
        self.assertIsNone(earlier["applied_fee"])
        self.assertEqual(later["applied_fee"], FEE)
        with self.writes() as session:
            statuses = session.scalars(
                select(EventRegistrationRow.status).where(
                    EventRegistrationRow.event_id == uuid.UUID(event_id)
                )
            ).all()
            fee_events = session.scalars(
                select(OutboxEventRow.event_type).where(
                    OutboxEventRow.event_type == "evento.tarifa_actualizada.v1"
                )
            ).all()
        self.assertEqual(sorted(statuses), ["REGISTERED", "REGISTERED"])
        self.assertEqual(len(fee_events), 1, "una versión nueva y un evento")

        # CA1: tras proyectar, el detalle público y «mis eventos» muestran la tarifa.
        self.project_through_pubsub(
            project_id, processed=5
        )  # creado, publicado, 2 inscritos, tarifa
        public = self.bff.get(f"/v1/events/{event_id}", headers=athlete)
        own = self.bff.get(f"/v1/events/{event_id}", headers=organizer)
        mine = self.bff.get("/v1/me/events", headers=organizer).json()["items"]
        self.assertEqual((public.json()["fee"], public.json()["registration_count"]), (FEE, None))
        self.assertEqual((own.json()["fee"], own.json()["registration_count"]), (FEE, 2))
        self.assertEqual(
            [(i["event_id"], i["fee"], i["registration_count"]) for i in mine],
            [(event_id, FEE, 2)],
        )
        with self.reads() as session:
            row = session.get(EventReadRow, uuid.UUID(event_id))
        assert row is not None
        self.assertEqual(
            (row.fee_amount, row.fee_currency, row.version), (Decimal("85000.00"), "COP", 3)
        )

        # Retirar la tarifa: `fee = null` proyectado; las inscripciones no cambian.
        removed = self.bff.delete(
            f"/v1/events/{event_id}/fee",
            params={"expected_version": "3"},
            headers={**organizer, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual((removed.status_code, removed.json()["fee"]), (200, None))
        self.project_through_pubsub(project_id, processed=6)
        self.assertIsNone(self.bff.get(f"/v1/events/{event_id}", headers=athlete).json()["fee"])
        with self.writes() as session:
            fees = session.scalars(
                select(EventRegistrationRow.applied_fee_amount).where(
                    EventRegistrationRow.event_id == uuid.UUID(event_id)
                )
            ).all()
        self.assertEqual(sorted(fees, key=str), sorted([None, Decimal("85000.00")], key=str))

    def test_actor_rejections_free_event_and_started_events(self) -> None:
        """Solo `organizer` con `events.pricing.manage`; `0.00` es gratuito (CA1); inicio
        vencido → `422 event_already_started` sin cambios (CA2)."""
        organizer = self.session_for("organizer")
        event_id = self.draft(organizer)
        for actor in ("athlete", "service_provider"):
            with self.subTest(actor=actor):
                denied = self.put_fee(
                    self.session_for(actor), event_id, {**FEE, "expected_version": 1}
                )
                self.assertEqual(
                    (denied.status_code, denied.json()["code"]), (403, "permission_required")
                )
        free = self.put_fee(
            organizer, event_id, {"amount": "0.00", "currency": "COP", "expected_version": 1}
        )
        self.assertEqual((free.status_code, free.json()["fee"]["amount"]), (200, "0.00"))
        with self.writes() as session, session.begin():
            session.execute(
                text(
                    "UPDATE event SET starts_at = now() - interval '1 hour', "
                    "ends_at = now() - interval '1 hour' + make_interval(mins => duration_minutes) "
                    "WHERE event_id = :id"
                ),
                {"id": event_id},
            )

        late = self.put_fee(organizer, event_id, {**FEE, "expected_version": 2})

        self.assertEqual((late.status_code, late.json()["code"]), (422, "validation_error"))
        self.assertEqual(
            late.json()["details"], [{"field": "starts_at", "issue": "event_already_started"}]
        )
        with self.writes() as session:
            count = session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.event_type == "evento.tarifa_actualizada.v1")
            )
        self.assertEqual(count, 1, "solo la tarifa gratuita")


if __name__ == "__main__":
    unittest.main()
