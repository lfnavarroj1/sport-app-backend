"""Componente HU036 en móvil: BFF → Usuarios (JWKS y contexto) → Afiliación (plan
`INTERMEDIATE`) → Entrenamientos (umbral, lote evaluado, `finish` con `RISK_STOP`) → outbox →
`entrenamientos-read-projector` → `session_monitoring_view` y `session_risk_alert_view`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`); reutiliza el montaje de
`test_device_flow`. Los eventos de la outbox se entregan al proyector real con el mismo sobre
que publica `entrenamientos-outbox-publisher` (el tramo por Pub/Sub lo cubre HU032). Recorrido
E2E de ADR-044: umbral bajo → alerta en la respuesta del lote → «Detener la práctica».
"""

import hashlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import test_device_flow
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_device_flow import utc_seconds

from training_management.application.messages import parse_training_event
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.models import (
    SessionMonitoringViewRow,
    SessionRiskAlertViewRow,
)
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import (
    OutboxEventRow,
    TrainingMeasurementRow,
    TrainingSessionRow,
)
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SETTINGS = "/v1/me/monitoring-settings"
_FLOW = test_device_flow.DeviceFlowTest


def samples(count: int, value: int) -> list[dict[str, Any]]:
    """`count` muestras de 1 Hz que terminan ahora."""
    end = datetime.now(UTC).replace(microsecond=0)
    return [
        {
            "measured_at": (end - timedelta(seconds=count - 1 - offset))
            .isoformat()
            .replace("+00:00", "Z"),
            "type": "HEART_RATE",
            "value": value,
            "unit": "bpm",
        }
        for offset in range(count)
    ]


class MonitoringFlowTest(unittest.TestCase):
    # Sin importar la clase: el descubrimiento volvería a ejecutar sus pruebas aquí.
    athlete = _FLOW.athlete
    upgrade_to_intermediate = _FLOW.upgrade_to_intermediate
    link = _FLOW.link
    send = _FLOW.send
    count = _FLOW.count

    def setUp(self) -> None:
        _FLOW.setUp(self)  # type: ignore[arg-type]
        read_engine = create_engine(self.urls["training_r"])
        self.addCleanup(read_engine.dispose)
        self.reads = sessionmaker(read_engine)
        self.projector = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.reads), clock=SystemClock()
        )

    def deliver_outbox(self) -> None:
        with sessionmaker(self.engine)() as session:
            rows = list(
                session.scalars(
                    select(OutboxEventRow).order_by(
                        OutboxEventRow.occurred_at, OutboxEventRow.aggregate_version
                    )
                )
            )
        for row in rows:
            envelope = build_envelope(
                ClaimedEvent(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    schema_version=row.schema_version,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    aggregate_version=row.aggregate_version,
                    occurred_at=row.occurred_at,
                    correlation_id=row.correlation_id,
                    causation_id=row.causation_id,
                    payload=row.payload,
                    attempts=0,
                )
            )
            self.projector(parse_training_event(envelope))

    def test_low_threshold_alert_then_risk_stop_through_the_mobile_channel(self) -> None:
        """CA1-CA4 por el canal móvil, con identidad, plan, persistencia y proyección reales."""
        athlete = self.athlete()
        self.upgrade_to_intermediate(athlete)
        auth = {"Authorization": athlete["Authorization"]}
        device_id = hashlib.sha256(f"C0:FF:EE:00:11:33{athlete['user_id']}".encode()).hexdigest()
        self.assertEqual(self.link(athlete, device_id, str(uuid.uuid4())).status_code, 201)

        default = self.bff.get(SETTINGS, headers=auth)
        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(
            {key: default.json()[key] for key in ("max_heart_rate_alert_bpm", "source")},
            {"max_heart_rate_alert_bpm": 171, "source": "FIXED_DEFAULT"},
        )
        self.assertIn("no es un diagnóstico médico", default.json()["disclaimer"])
        low = self.bff.put(SETTINGS, json={"max_heart_rate_alert_bpm": 100}, headers=auth)
        self.assertEqual((low.status_code, low.json()["source"]), (200, "CUSTOM"))

        session_id = str(uuid.uuid4())
        started = self.bff.post(
            "/v1/training-sessions",
            json={
                "session_id": session_id,
                "sport": "running",
                "started_at": utc_seconds(-timedelta(minutes=5)),
            },
            headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(started.status_code, 201, started.text)

        batch = {"device_id": device_id, "samples": samples(15, 130)}
        risky = self.send(athlete, session_id, batch)
        self.assertEqual(risky.status_code, 200, risky.text)
        evaluation = risky.json()["evaluation"]
        self.assertEqual(evaluation["status"], "RISK")
        (alert,) = evaluation["alerts"]
        self.assertEqual((alert["rule"], alert["severity"]), ("HR_ABOVE_THRESHOLD_V1", "HIGH"))
        self.assertIn("no es un diagnóstico médico", alert["message"])

        stop = {"ended_at": utc_seconds(timedelta(0)), "reason": "RISK_STOP"}
        finished = self.bff.post(
            f"/v1/training-sessions/{session_id}/finish",
            json=stop,
            headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(
            (finished.json()["status"], finished.json()["end_reason"]), ("FINISHED", "RISK_STOP")
        )
        again = self.bff.post(
            f"/v1/training-sessions/{session_id}/finish",
            json=stop,
            headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(again.json(), finished.json())
        with sessionmaker(self.engine)() as session:
            row = session.get(TrainingSessionRow, uuid.UUID(session_id))
        assert row is not None
        self.assertEqual(row.end_reason, "RISK_STOP")
        self.assertEqual(self.count(TrainingMeasurementRow), 15)

        self.deliver_outbox()
        with self.reads() as session:
            view = session.get(SessionMonitoringViewRow, uuid.UUID(session_id))
            alerts = session.scalars(select(SessionRiskAlertViewRow)).all()
        assert view is not None
        self.assertEqual((view.samples_count, view.max_value, view.version), (15, 130, 1))
        self.assertEqual([str(item.alert_id) for item in alerts], [alert["alert_id"]])

    def test_free_athlete_has_no_monitoring(self) -> None:
        """CA5: sin `training.realtime.monitor`, `403 capability_required`."""
        athlete = self.athlete()
        auth = {"Authorization": athlete["Authorization"]}
        for response in (
            self.bff.get(SETTINGS, headers=auth),
            self.bff.put(SETTINGS, json={"max_heart_rate_alert_bpm": 150}, headers=auth),
        ):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )


if __name__ == "__main__":
    unittest.main()
