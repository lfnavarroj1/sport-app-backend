"""API de HU037 en el servicio: dispositivos (CA1, CA6) e ingestión (CA3-CA6) con dobles."""

import logging
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from device_fakes import (
    InMemoryDeviceReader,
    InMemoryDeviceStore,
    InMemoryDeviceUnitOfWork,
    InMemoryMeasurementUnitOfWork,
    device_body,
    device_hash,
    sample,
)
from fakes import NOW, FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import JwksUnavailable
from training_management.api.devices import build_devices_router
from training_management.api.measurements import build_measurements_router
from training_management.application.commands.devices import RegisterDevice, RevokeDevice
from training_management.application.commands.measurements import IngestMeasurements
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.queries.devices import ListMyDevices
from training_management.domain.devices.model import (
    AthleteDevice,
    DeviceData,
    DeviceProtocol,
    DeviceType,
)
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import StartData, TrainingSession

DEVICES = "/v1/me/devices"
CONNECT = "devices.measurement.connect"
MONITOR = "training.realtime.monitor"


def at(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


class SlowAffiliation(FakeCapabilities):
    def has_capability(self, user_id: Any, capability: str) -> bool:
        raise CapabilitiesTimeout()


class DevicesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryDeviceStore()
        self.athlete_id = uuid4()
        self.device_id = device_hash("AA:BB:CC:DD:EE:FF", self.athlete_id)
        self.capabilities = FakeCapabilities({self.athlete_id: {CONNECT, MONITOR}})
        self.clock = FixedClock()
        self.before_commit: Any = None
        self.jwks_down = False
        self.client = self.client_with(self.capabilities)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        def before_commit() -> None:
            # Se ejecuta una vez, en el primer COMMIT (la consulta de la clave no confirma).
            hook, self.before_commit = self.before_commit, None
            if hook is not None:
                hook()

        def unit_of_work() -> InMemoryDeviceUnitOfWork:
            return InMemoryDeviceUnitOfWork(self.store, before_commit)

        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_devices_router(
                    verify_token=verify,
                    register=RegisterDevice(
                        unit_of_work=unit_of_work, capabilities=capabilities, clock=self.clock
                    ),
                    revoke=RevokeDevice(
                        unit_of_work=unit_of_work, capabilities=capabilities, clock=self.clock
                    ),
                    list_devices=ListMyDevices(
                        reader=InMemoryDeviceReader(self.store), capabilities=capabilities
                    ),
                ),
                build_measurements_router(
                    verify_token=verify,
                    ingest=IngestMeasurements(
                        unit_of_work=lambda: InMemoryMeasurementUnitOfWork(self.store),
                        capabilities=capabilities,
                        clock=self.clock,
                    ),
                ),
            ],
        )
        return TestClient(app)

    def client_with_store(self, store: InMemoryDeviceStore) -> TestClient:
        """Otra instancia del servicio sobre su propio almacén (la solicitud ganadora)."""
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_devices_router(
                    verify_token=VERIFIER.verify,
                    register=RegisterDevice(
                        unit_of_work=lambda: InMemoryDeviceUnitOfWork(store),
                        capabilities=self.capabilities,
                        clock=self.clock,
                    ),
                    revoke=RevokeDevice(
                        unit_of_work=lambda: InMemoryDeviceUnitOfWork(store),
                        capabilities=self.capabilities,
                        clock=self.clock,
                    ),
                    list_devices=ListMyDevices(
                        reader=InMemoryDeviceReader(store), capabilities=self.capabilities
                    ),
                )
            ],
        )
        return TestClient(app)

    def register(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(DEVICES, json=body, headers=sent)

    def ingest(self, session_id: Any, body: Any, **headers: str) -> Any:
        return self.client.post(
            f"/v1/training-sessions/{session_id}/measurements",
            json=body,
            headers={**self.auth, **headers},
        )

    def start_session(self, athlete_id: Any = None) -> TrainingSession:
        session = TrainingSession.start(
            athlete_id=athlete_id or self.athlete_id,
            data=StartData(uuid4(), None, Sport.RUNNING, NOW - timedelta(minutes=10)),
            now=NOW - timedelta(minutes=10),
        )
        self.store.sessions[session.session_id] = session
        return session

    def linked(self) -> None:
        self.assertEqual(self.register(device_body(self.device_id), str(uuid4())).status_code, 201)

    # ------------------------------------------------------------ vinculación (CA1)

    def test_registers_an_active_device(self) -> None:
        response = self.register(device_body(self.device_id, display_name=" Banda "), str(uuid4()))
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(response.headers["Location"], f"{DEVICES}/{self.device_id}")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(
            {key: body[key] for key in ("device_id", "display_name", "device_type", "protocol")},
            {
                "device_id": self.device_id,
                "display_name": "Banda",
                "device_type": "HEART_RATE_MONITOR",
                "protocol": "BLE_HRS",
            },
        )
        self.assertEqual((body["status"], body["version"]), ("ACTIVE", 1))
        self.assertEqual(body["athlete_id"], str(self.athlete_id))
        self.assertIsNone(body["revoked_at"])
        self.assertTrue(body["created_at"].endswith("Z"))
        self.assertIn((self.athlete_id, CONNECT), self.capabilities.calls)

    def test_same_key_replays_the_original_result(self) -> None:
        key = str(uuid4())
        first = self.register(device_body(self.device_id), key)
        second = self.register(device_body(self.device_id), key)
        self.assertEqual((second.status_code, second.headers["Idempotent-Replayed"]), (201, "true"))
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.store.devices), 1)

    def test_same_active_link_with_another_key_is_returned_unchanged(self) -> None:
        self.linked()
        again = self.register(device_body(self.device_id), str(uuid4()))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["version"], 1)
        self.assertNotIn("Location", again.headers)

    def test_another_name_for_an_active_device_is_rejected(self) -> None:
        key = str(uuid4())
        self.assertEqual(self.register(device_body(self.device_id), key).status_code, 201)
        for sent_key in (key, str(uuid4())):
            with self.subTest(same_key=sent_key == key):
                response = self.register(device_body(self.device_id, display_name="Otra"), sent_key)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "device_already_registered")
        self.assertEqual(
            self.store.devices[(self.athlete_id, self.device_id)].display_name, "Banda pecho"
        )

    def test_same_key_with_another_device_is_reused(self) -> None:
        key = str(uuid4())
        self.register(device_body(self.device_id), key)
        other = device_hash("11:22:33:44:55:66", self.athlete_id)
        response = self.register(device_body(other), key)
        self.assertEqual(
            (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
        )

    def test_expired_key_can_be_used_again(self) -> None:
        key = str(uuid4())
        self.register(device_body(self.device_id), key)
        self.clock.advance(timedelta(hours=25))
        other = device_hash("11:22:33:44:55:66", self.athlete_id)
        self.assertEqual(self.register(device_body(other), key).status_code, 201)

    def test_a_revoked_device_can_be_linked_again(self) -> None:
        self.linked()
        self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        again = self.register(device_body(self.device_id, display_name="Banda nueva"), str(uuid4()))
        self.assertEqual(again.status_code, 201)
        self.assertEqual(
            (again.json()["status"], again.json()["version"], again.json()["display_name"]),
            ("ACTIVE", 3, "Banda nueva"),
        )
        self.assertIsNone(again.json()["revoked_at"])

    def test_concurrent_link_of_the_same_device_is_evaluated_against_the_winner(self) -> None:
        def winner() -> None:
            data = DeviceData(
                self.device_id, "Banda pecho", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
            )
            device = AthleteDevice.register(athlete_id=self.athlete_id, data=data, now=NOW)
            self.store.devices[(self.athlete_id, self.device_id)] = device

        self.before_commit = winner
        response = self.register(device_body(self.device_id), str(uuid4()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.devices), 1)

    def test_concurrent_link_with_the_same_key_replays_the_winner(self) -> None:
        """Reintento del móvil mientras la primera solicitud sigue en curso."""
        key = str(uuid4())
        winner_store = InMemoryDeviceStore()
        winner = self.client_with_store(winner_store)

        def first_request_commits() -> None:
            winner.post(
                DEVICES,
                json=device_body(self.device_id),
                headers={**self.auth, "Idempotency-Key": key},
            )
            self.store.devices.update(winner_store.devices)
            self.store.commands.update(winner_store.commands)

        self.before_commit = first_request_commits
        response = self.register(device_body(self.device_id), key)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.devices), 1)

    def test_unchanged_result_is_remembered_by_its_key(self) -> None:
        self.linked()
        key = str(uuid4())
        unchanged = self.register(device_body(self.device_id), key)
        self.assertEqual(unchanged.status_code, 200)
        self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        retried = self.register(device_body(self.device_id), key)
        self.assertEqual(
            (retried.status_code, retried.headers["Idempotent-Replayed"]), (200, "true")
        )
        self.assertEqual(retried.json(), unchanged.json())
        self.assertEqual(self.store.devices[(self.athlete_id, self.device_id)].status, "REVOKED")

    def test_validation_errors_report_every_rule(self) -> None:
        response = self.register(
            {
                "device_id": "AA:BB:CC:DD:EE:FF",
                "display_name": "x" * 61,
                "device_type": "SCALE",
                "protocol": 3,
                "athlete_id": str(uuid4()),
            },
            "no-uuid",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            {(item["field"], item["issue"]) for item in response.json()["details"]},
            {
                ("Idempotency-Key", "invalid_uuid"),
                ("athlete_id", "field_not_allowed"),
                ("protocol", "unknown_value"),
                ("device_id", "invalid_format"),
                ("display_name", "invalid_length"),
                ("device_type", "unknown_value"),
            },
        )
        self.assertEqual(self.store.devices, {})

    def test_missing_key_and_body(self) -> None:
        response = self.register(None)
        self.assertEqual(
            [(item["field"], item["issue"]) for item in response.json()["details"]],
            [("Idempotency-Key", "required"), (None, "required")],
        )
        missing = self.register({"device_id": self.device_id}, str(uuid4()))
        self.assertEqual(
            {(item["field"], item["issue"]) for item in missing.json()["details"]},
            {("display_name", "required"), ("device_type", "required"), ("protocol", "required")},
        )

    # ------------------------------------------------------------ acceso

    def test_only_authenticated_athletes_with_the_capability(self) -> None:
        anonymous = self.client.get(DEVICES)
        self.assertEqual(
            (anonymous.status_code, anonymous.json()["code"]), (401, "invalid_access_token")
        )
        organizer = {"Authorization": f"Bearer {token(uuid4(), 'organizer')}"}
        for method, path in (("get", DEVICES), ("delete", f"{DEVICES}/{self.device_id}")):
            response = self.client.request(method.upper(), path, headers=organizer)
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        response = self.register(device_body(self.device_id), str(uuid4()), **organizer)
        self.assertEqual(response.json()["code"], "permission_required")
        self.capabilities.granted[self.athlete_id] = {MONITOR}
        for response in (
            self.register(device_body(self.device_id), str(uuid4())),
            self.client.get(DEVICES, headers=self.auth),
            self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth),
        ):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )

    def test_affiliation_failures(self) -> None:
        self.capabilities.unavailable = True
        self.assertEqual(self.client.get(DEVICES, headers=self.auth).status_code, 503)
        slow = self.client_with(SlowAffiliation())
        response = slow.post(
            DEVICES,
            json=device_body(self.device_id),
            headers={**self.auth, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual((response.status_code, response.json()["code"]), (504, "upstream_timeout"))
        self.jwks_down = True
        self.assertEqual(self.client.get(DEVICES, headers=self.auth).status_code, 503)

    # ------------------------------------------------------------ consulta y revocación (CA6)

    def test_lists_own_devices_newest_first(self) -> None:
        self.linked()
        self.clock.advance(timedelta(minutes=1))
        second = device_hash("11:22:33:44:55:66", self.athlete_id)
        self.register(device_body(second, display_name="Reloj"), str(uuid4()))
        stranger = uuid4()
        self.store.devices[(stranger, self.device_id)] = AthleteDevice.register(
            athlete_id=stranger,
            data=DeviceData(
                self.device_id, "Ajeno", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
            ),
            now=NOW,
        )
        response = self.client.get(DEVICES, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            [item["display_name"] for item in response.json()["items"]], ["Reloj", "Banda pecho"]
        )

    def test_revocation_is_idempotent(self) -> None:
        self.linked()
        self.clock.advance(timedelta(minutes=1))
        first = self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual((first.json()["status"], first.json()["version"]), ("REVOKED", 2))
        self.assertEqual(first.json()["revoked_at"], at(timedelta(minutes=1)))
        second = self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        self.assertEqual((second.status_code, second.json()), (200, first.json()))
        listed = self.client.get(DEVICES, headers=self.auth).json()["items"]
        self.assertEqual([item["status"] for item in listed], ["REVOKED"])

    def test_revoking_unknown_or_foreign_devices(self) -> None:
        stranger = uuid4()
        self.store.devices[(stranger, self.device_id)] = AthleteDevice.register(
            athlete_id=stranger,
            data=DeviceData(
                self.device_id, "Ajeno", DeviceType.HEART_RATE_MONITOR, DeviceProtocol.BLE_HRS
            ),
            now=NOW,
        )
        response = self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "device_not_found"))
        self.assertEqual(self.store.devices[(stranger, self.device_id)].status, "ACTIVE")
        invalid = self.client.delete(f"{DEVICES}/AA:BB", headers=self.auth)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["details"], [{"field": "device_id", "issue": "invalid_format"}]
        )

    # ------------------------------------------------------------ ingestión (CA3-CA6)

    def test_batch_is_validated_per_sample(self) -> None:
        self.linked()
        session = self.start_session()
        response = self.ingest(
            session.session_id,
            {
                "device_id": self.device_id,
                "samples": [
                    sample(at(-timedelta(seconds=4)), 121),
                    sample(at(-timedelta(seconds=3)), 300),
                    sample(at(-timedelta(seconds=2)), 123, unit="bps"),
                    sample(at(timedelta(seconds=30)), 124),
                    sample(at(-timedelta(seconds=1)), 125),
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "session_id": str(session.session_id),
                "device_id": self.device_id,
                "received": 5,
                "accepted": 2,
                "duplicates": 0,
                "rejected": [
                    {"index": 1, "issue": "out_of_range"},
                    {"index": 2, "issue": "invalid_unit"},
                    {"index": 3, "issue": "too_far_in_future"},
                ],
            },
        )
        self.assertEqual(
            sorted(item.value for item in self.store.measurements.values()), [121, 125]
        )
        self.assertIn((self.athlete_id, MONITOR), self.capabilities.calls)

    def test_resent_and_out_of_order_batches(self) -> None:
        """CA4 y CA5: el reenvío solo suma duplicados y el desorden se acepta."""
        self.linked()
        session = self.start_session()
        batch = {
            "device_id": self.device_id,
            "samples": [sample(at(-timedelta(seconds=s)), 100 + s) for s in (5, 4, 3, 2, 1)],
        }
        first = self.ingest(session.session_id, batch).json()
        self.assertEqual((first["accepted"], first["duplicates"]), (5, 0))
        again = self.ingest(session.session_id, batch).json()
        self.assertEqual((again["accepted"], again["duplicates"], again["rejected"]), (0, 5, []))
        older = {
            "device_id": self.device_id,
            "samples": [
                sample(at(-timedelta(seconds=10)), 90),
                sample(at(-timedelta(seconds=3)), 90),
                sample(at(-timedelta(seconds=10)), 91),
            ],
        }
        result = self.ingest(session.session_id, older).json()
        self.assertEqual((result["accepted"], result["duplicates"]), (1, 2))
        self.assertEqual(len(self.store.measurements), 6)

    def test_revoked_device_stops_ingestion(self) -> None:
        """CA6."""
        self.linked()
        session = self.start_session()
        self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        response = self.ingest(
            session.session_id,
            {"device_id": self.device_id, "samples": [sample(at(timedelta(0)))]},
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "device_revoked"))
        self.assertEqual(self.store.measurements, {})

    def test_session_and_device_must_be_own_and_active(self) -> None:
        self.linked()
        batch = {"device_id": self.device_id, "samples": [sample(at(timedelta(0)))]}
        foreign = self.start_session(uuid4())
        for session_id in (uuid4(), foreign.session_id):
            response = self.ingest(session_id, batch)
            self.assertEqual(
                (response.status_code, response.json()["code"]), (404, "training_session_not_found")
            )
        finished = self.start_session()
        self.store.sessions[finished.session_id] = finished.finish(ended_at=NOW, now=NOW)
        response = self.ingest(finished.session_id, batch)
        self.assertEqual(
            (response.status_code, response.json()["code"]),
            (409, "training_session_already_finished"),
        )
        active = self.start_session()
        unknown = {**batch, "device_id": device_hash("00:00:00:00:00:00", self.athlete_id)}
        response = self.ingest(active.session_id, unknown)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "device_not_found"))
        self.assertEqual(self.store.measurements, {})

    def test_batch_shape_and_access(self) -> None:
        response = self.ingest("abc", {"device_id": "x", "samples": []})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["details"],
            [
                {"field": "session_id", "issue": "invalid_uuid"},
                {"field": "device_id", "issue": "invalid_format"},
                {"field": "samples", "issue": "required"},
            ],
        )
        batch = {"device_id": self.device_id, "samples": [sample(at(timedelta(0)))]}
        organizer = {"Authorization": f"Bearer {token(uuid4(), 'organizer')}"}
        self.assertEqual(
            self.ingest(uuid4(), batch, **organizer).json()["code"], "permission_required"
        )
        self.capabilities.granted[self.athlete_id] = {CONNECT}
        response = self.ingest(uuid4(), batch)
        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "capability_required")
        )
        self.capabilities.unavailable = True
        self.assertEqual(self.ingest(uuid4(), batch).status_code, 503)
        slow = self.client_with(SlowAffiliation())
        response = slow.post(
            f"/v1/training-sessions/{uuid4()}/measurements", json=batch, headers=self.auth
        )
        self.assertEqual((response.status_code, response.json()["code"]), (504, "upstream_timeout"))

    def test_logs_never_contain_heart_rate_values_or_device_identity(self) -> None:
        self.linked()
        session = self.start_session()
        with self.assertLogs("sportapp.audit", level=logging.INFO) as captured:
            self.ingest(
                session.session_id,
                {"device_id": self.device_id, "samples": [sample(at(timedelta(0)), 187)]},
            )
            self.client.delete(f"{DEVICES}/{self.device_id}", headers=self.auth)
        fields = [record.__dict__.get("fields", {}) for record in captured.records]
        self.assertEqual(captured.records[0].getMessage(), "audit.measurements_ingested")
        self.assertEqual(
            set(fields[0]),
            {
                "athlete_id",
                "result",
                "session_id",
                "received",
                "accepted",
                "duplicates",
                "rejected",
            },
        )
        values = [str(value) for item in fields for value in item.values()]
        self.assertNotIn("187", values)
        self.assertFalse(any(self.device_id in value or "Banda" in value for value in values))


if __name__ == "__main__":
    unittest.main()
