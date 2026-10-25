"""HU036 en el servicio con dobles: evaluación en la respuesta del lote (CA1-CA3), eventos en
la outbox, umbral (`/v1/me/monitoring-settings`) y elegibilidad (CA5)."""

import logging
import unittest
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

from device_fakes import (
    FakeBirthDates,
    InMemoryDeviceStore,
    InMemoryMeasurementUnitOfWork,
    InMemoryMonitoringSettingsUnitOfWork,
    custom_settings,
    device_hash,
)
from fakes import NOW, FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.measurements import build_measurements_router
from training_management.api.monitoring_settings import build_monitoring_settings_router
from training_management.application.commands.measurements import IngestMeasurements
from training_management.application.commands.monitoring_settings import (
    UpdateMonitoringSettings,
)
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.application.ports import Caller
from training_management.application.queries.monitoring_settings import GetMonitoringSettings
from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceData,
    DeviceProtocol,
    DeviceType,
)
from training_management.domain.monitoring.model import ALERT_INSTRUCTION, ALERT_MESSAGE
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import StartData, TrainingSession

MONITOR = "training.realtime.monitor"
SETTINGS = "/v1/me/monitoring-settings"
DISCLAIMER = "Esto no es un diagnóstico médico."


def at(seconds: int) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def samples(first: int, count: int, value: int) -> list[dict[str, Any]]:
    """`count` muestras de 1 Hz desde NOW + `first` s."""
    return [
        {"measured_at": at(first + offset), "type": "HEART_RATE", "value": value, "unit": "bpm"}
        for offset in range(count)
    ]


class MonitoringApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryDeviceStore()
        self.athlete_id = uuid4()
        self.device_id = device_hash("AA:BB:CC:DD:EE:01", self.athlete_id)
        self.capabilities = FakeCapabilities({self.athlete_id: {MONITOR}})
        self.birth_dates = FakeBirthDates()
        self.clock = FixedClock()
        self.ids: list[UUID] = []
        self.client = self.client_with(self.capabilities)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}
        device = AthleteDevice.register(
            athlete_id=self.athlete_id,
            data=DeviceData(
                self.device_id, "Banda", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
            ),
            now=NOW - timedelta(hours=1),
        )
        self.store.devices[(self.athlete_id, self.device_id)] = device
        self.session = TrainingSession.start(
            athlete_id=self.athlete_id,
            data=StartData(uuid4(), None, Sport.RUNNING, NOW - timedelta(minutes=10)),
            now=NOW - timedelta(minutes=10),
        )
        self.store.sessions[self.session.session_id] = self.session

    def new_id(self) -> UUID:
        value = uuid4()
        self.ids.append(value)
        return value

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        def settings_unit() -> InMemoryMonitoringSettingsUnitOfWork:
            return InMemoryMonitoringSettingsUnitOfWork(self.store)

        thresholds = ResolveThreshold(
            unit_of_work=settings_unit, birth_dates=self.birth_dates, clock=self.clock
        )
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_measurements_router(
                    verify_token=VERIFIER.verify,
                    ingest=IngestMeasurements(
                        unit_of_work=lambda: InMemoryMeasurementUnitOfWork(self.store),
                        capabilities=capabilities,
                        thresholds=thresholds,
                        clock=self.clock,
                        new_id=self.new_id,
                    ),
                ),
                build_monitoring_settings_router(
                    verify_token=VERIFIER.verify,
                    get_settings=GetMonitoringSettings(
                        resolve=thresholds, capabilities=capabilities
                    ),
                    update_settings=UpdateMonitoringSettings(
                        unit_of_work=settings_unit, capabilities=capabilities, clock=self.clock
                    ),
                ),
            ],
        )
        return TestClient(app)

    def ingest(self, batch: list[dict[str, Any]]) -> Any:
        response = self.client.post(
            f"/v1/training-sessions/{self.session.session_id}/measurements",
            json={"device_id": self.device_id, "samples": batch},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def events(self, event_type: str) -> list[Any]:
        return [item for item in self.store.outbox if item.event_type == event_type]

    # ------------------------------------------------------------ evaluación (CA1-CA3)

    def test_normal_batch_is_ok_and_publishes_a_summary(self) -> None:
        """CA1: aceptación por muestra y resumen `medicion.recibida.v1` por lote."""
        body = self.ingest(samples(-4, 5, 120))
        self.assertEqual(
            (body["accepted"], body["duplicates"], body["evaluation"]),
            (5, 0, {"status": "OK", "alerts": []}),
        )
        (event,) = self.events("medicion.recibida.v1")
        self.assertEqual(
            (event.aggregate_type, event.aggregate_id, event.aggregate_version),
            ("training_session", self.session.session_id, 1),
        )
        self.assertEqual(
            event.payload,
            {
                "session_id": str(self.session.session_id),
                "device_id": self.device_id,
                "window_from": at(-4),
                "window_to": at(0),
                "samples_accepted": 5,
                "last_value": 120,
                "max_value": 120,
                "avg_value": "120.0",
                "version": 1,
            },
        )
        self.assertIsNone(event.causation_id)

    def test_fifteen_seconds_above_threshold_alerts_in_the_same_response(self) -> None:
        """CA2: `RISK` con la alerta, su instrucción y el aviso no diagnóstico."""
        self.store.settings[self.athlete_id] = custom_settings(self.athlete_id, 150, NOW)
        first = self.ingest(samples(-14, 10, 160))
        self.assertEqual(first["evaluation"], {"status": "OK", "alerts": []})
        second = self.ingest(samples(-4, 5, 160))
        evaluation = second["evaluation"]
        self.assertEqual(evaluation["status"], "RISK")
        (alert,) = evaluation["alerts"]
        self.assertEqual(
            {key: alert[key] for key in ("rule", "severity", "message", "instruction")},
            {
                "rule": "HR_ABOVE_THRESHOLD_V1",
                "severity": "HIGH",
                "message": ALERT_MESSAGE,
                "instruction": ALERT_INSTRUCTION,
            },
        )
        self.assertIn(DISCLAIMER, alert["message"])
        self.assertEqual(alert["detected_at"], at(0))
        (risk,) = self.events("riesgo.detectado.v1")
        summaries = self.events("medicion.recibida.v1")
        self.assertEqual([item.aggregate_version for item in summaries], [1, 2])
        self.assertEqual((risk.aggregate_id, risk.aggregate_version), (self.session.session_id, 1))
        self.assertEqual(risk.causation_id, str(summaries[-1].event_id))
        self.assertEqual(risk.payload["alert_id"], alert["alert_id"])
        self.assertNotIn("value", risk.payload)

    def test_alert_is_not_repeated_before_sixty_seconds(self) -> None:
        """CA2: mientras persiste, la alerta repite como máximo cada 60 s."""
        self.store.settings[self.athlete_id] = custom_settings(self.athlete_id, 150, NOW)
        self.clock.current = NOW - timedelta(seconds=60)
        responses = []
        for start in range(-74, 1, 5):
            self.clock.current = NOW + timedelta(seconds=start + 4)
            responses.append(self.ingest(samples(start, 5, 170))["evaluation"])
        alerts = [alert["detected_at"] for item in responses for alert in item["alerts"]]
        self.assertEqual(alerts, [at(-60), at(0)])
        self.assertEqual({item["status"] for item in responses[3:]}, {"RISK"})
        self.assertEqual(len(self.events("riesgo.detectado.v1")), 2)

    def test_resending_a_batch_repeats_neither_summary_nor_alert(self) -> None:
        self.store.settings[self.athlete_id] = custom_settings(self.athlete_id, 150, NOW)
        batch = samples(-14, 15, 170)
        first = self.ingest(batch)
        again = self.ingest(batch)
        self.assertEqual(len(first["evaluation"]["alerts"]), 1)
        self.assertEqual(
            (again["accepted"], again["duplicates"], again["evaluation"]),
            (0, 15, {"status": "RISK", "alerts": []}),
        )
        self.assertEqual(len(self.events("medicion.recibida.v1")), 1)
        self.assertEqual(len(self.events("riesgo.detectado.v1")), 1)

    def test_no_data_after_thirty_seconds_without_samples(self) -> None:
        """CA3: el último valor no se presenta como actual."""
        self.ingest(samples(-35, 5, 190))
        self.clock.current = NOW
        stale = self.ingest(samples(-35, 5, 190))
        self.assertEqual(stale["evaluation"], {"status": "NO_DATA", "alerts": []})
        old_batch = self.ingest(samples(-50, 5, 190))
        self.assertEqual(old_batch["evaluation"]["status"], "NO_DATA")

    def test_all_rejected_samples_without_history_is_no_data(self) -> None:
        body = self.ingest(
            [{"measured_at": at(0), "type": "HEART_RATE", "value": 10, "unit": "bpm"}]
        )
        self.assertEqual(body["evaluation"], {"status": "NO_DATA", "alerts": []})
        self.assertEqual(self.store.outbox, [])

    def test_default_threshold_uses_the_age_from_users(self) -> None:
        # 40 años: umbral 162.
        self.birth_dates.dates[self.athlete_id] = date(1986, 1, 1)
        body = self.ingest(samples(-14, 15, 165))
        self.assertEqual(body["evaluation"]["status"], "RISK")
        self.assertEqual(self.birth_dates.calls, 1)

    def test_users_down_without_own_threshold_is_unavailable(self) -> None:
        self.birth_dates.down = True
        response = self.client.post(
            f"/v1/training-sessions/{self.session.session_id}/measurements",
            json={"device_id": self.device_id, "samples": samples(-4, 5, 120)},
            headers=self.auth,
        )
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual(self.store.measurements, {})
        # Con umbral propio no se consulta Usuarios.
        self.store.settings[self.athlete_id] = custom_settings(self.athlete_id, 150, NOW)
        self.assertEqual(self.ingest(samples(-4, 5, 120))["accepted"], 5)

    def test_audit_has_only_counts_and_status(self) -> None:
        self.store.settings[self.athlete_id] = custom_settings(self.athlete_id, 150, NOW)
        with self.assertLogs("sportapp.audit", level=logging.INFO) as captured:
            self.ingest(samples(-14, 15, 187))
        fields = captured.records[0].__dict__["fields"]
        self.assertEqual((fields["evaluation"], fields["alerts"]), ("RISK", 1))
        values = [str(value) for value in fields.values()]
        self.assertFalse(any("187" in value or "150" in value for value in values))

    # ------------------------------------------------------------ umbral

    def test_get_returns_the_default_with_its_source_and_disclaimer(self) -> None:
        response = self.client.get(SETTINGS, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "max_heart_rate_alert_bpm": 171,
                "source": "FIXED_DEFAULT",
                "updated_at": None,
                "disclaimer": DISCLAIMER,
            },
        )
        self.birth_dates.dates[self.athlete_id] = date(2001, 10, 24)
        body = self.client.get(SETTINGS, headers=self.auth).json()
        self.assertEqual((body["max_heart_rate_alert_bpm"], body["source"]), (176, "AGE_DEFAULT"))

    def test_put_sets_the_threshold_idempotently(self) -> None:
        first = self.client.put(SETTINGS, json={"max_heart_rate_alert_bpm": 155}, headers=self.auth)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(
            {key: first.json()[key] for key in ("max_heart_rate_alert_bpm", "source")},
            {"max_heart_rate_alert_bpm": 155, "source": "CUSTOM"},
        )
        self.assertTrue(first.json()["updated_at"].endswith("Z"))
        self.clock.advance(timedelta(minutes=5))
        again = self.client.put(SETTINGS, json={"max_heart_rate_alert_bpm": 155}, headers=self.auth)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(self.store.settings[self.athlete_id].version, 1)
        changed = self.client.put(
            SETTINGS, json={"max_heart_rate_alert_bpm": 140}, headers=self.auth
        )
        self.assertEqual(changed.json()["max_heart_rate_alert_bpm"], 140)
        self.assertEqual(self.store.settings[self.athlete_id].version, 2)
        self.assertEqual(self.client.get(SETTINGS, headers=self.auth).json(), changed.json())

    def test_concurrent_first_put_retries_on_the_existing_row(self) -> None:
        winner = custom_settings(self.athlete_id, 200, NOW)

        def settings_unit() -> InMemoryMonitoringSettingsUnitOfWork:
            def lose() -> None:
                self.store.settings[self.athlete_id] = winner

            hook = lose if self.athlete_id not in self.store.settings else None
            return InMemoryMonitoringSettingsUnitOfWork(self.store, hook)

        update = UpdateMonitoringSettings(
            unit_of_work=settings_unit, capabilities=self.capabilities, clock=self.clock
        )

        result = update(Caller(self.athlete_id, "athlete"), {"max_heart_rate_alert_bpm": 160})
        self.assertEqual((result.value, self.store.settings[self.athlete_id].version), (160, 2))

    def test_put_validation(self) -> None:
        response = self.client.put(
            SETTINGS, json={"max_heart_rate_alert_bpm": 230, "x": 1}, headers=self.auth
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            {(item["field"], item["issue"]) for item in response.json()["details"]},
            {("max_heart_rate_alert_bpm", "out_of_range"), ("x", "field_not_allowed")},
        )
        empty = self.client.put(SETTINGS, headers=self.auth)
        self.assertEqual(empty.json()["details"], [{"field": None, "issue": "required"}])

    def test_users_down_for_the_default_is_unavailable(self) -> None:
        self.birth_dates.down = True
        response = self.client.get(SETTINGS, headers=self.auth)
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    # ------------------------------------------------------------ elegibilidad (CA5)

    def test_capability_and_actor_are_required(self) -> None:
        """CA5: sin `training.realtime.monitor`, `403 capability_required`."""
        free = self.client_with(FakeCapabilities())
        requests = [
            free.get(SETTINGS, headers=self.auth),
            free.put(SETTINGS, json={"max_heart_rate_alert_bpm": 150}, headers=self.auth),
            free.post(
                f"/v1/training-sessions/{self.session.session_id}/measurements",
                json={"device_id": self.device_id, "samples": samples(-4, 5, 120)},
                headers=self.auth,
            ),
        ]
        for response in requests:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        organizer = {"Authorization": f"Bearer {token(uuid4(), 'organizer')}"}
        self.assertEqual(
            self.client.get(SETTINGS, headers=organizer).json()["code"], "permission_required"
        )
        self.assertEqual(self.client.get(SETTINGS).status_code, 401)
        self.assertEqual(self.store.settings, {})


if __name__ == "__main__":
    unittest.main()
