"""BFF móvil, HU036: evaluación en la respuesta del lote, umbral (`/v1/me/monitoring-settings`)
y `finish` con `RISK_STOP` (ADR-044).

El borde del umbral es el de la ingestión: `athlete` con `training.realtime.monitor` (CA5). El
BFF no valida el umbral ni evalúa riesgos: reenvía y entrega la respuesta del servicio sin
alterar. Reutiliza el montaje de `test_devices` (mismo router y dobles).
"""

import json
import unittest
import uuid

import httpx
import test_devices
from test_devices import (
    BATCH,
    DEVICE_ID,
    MEASUREMENTS_PATH,
    REALTIME,
    SESSION_ID,
    error,
)

SETTINGS_PATH = "/v1/me/monitoring-settings"
SETTINGS = {
    "max_heart_rate_alert_bpm": 171,
    "source": "FIXED_DEFAULT",
    "updated_at": None,
    "disclaimer": "Esto no es un diagnóstico médico.",
}
RISK_RESULT = {
    "session_id": SESSION_ID,
    "device_id": DEVICE_ID,
    "received": 5,
    "accepted": 5,
    "duplicates": 0,
    "rejected": [],
    "evaluation": {
        "status": "RISK",
        "alerts": [
            {
                "alert_id": str(uuid.uuid4()),
                "rule": "HR_ABOVE_THRESHOLD_V1",
                "severity": "HIGH",
                "message": "Tu frecuencia cardiaca supera el límite que configuraste. Reduce la "
                "intensidad o detén la práctica. Esto no es un diagnóstico médico.",
                "instruction": "Reduce la intensidad o detén la práctica.",
                "detected_at": "2026-10-24T12:30:09Z",
            }
        ],
    },
}


class MonitoringRoutesTest(unittest.TestCase):
    # Sin importar la clase: el descubrimiento volvería a ejecutar sus pruebas aquí.
    setUp = test_devices.DevicesRoutesTest.setUp
    auth = test_devices.DevicesRoutesTest.auth

    def test_risk_evaluation_reaches_the_phone_unchanged(self) -> None:
        """CA2: la alerta llega en la misma respuesta del lote."""
        self.reply = httpx.Response(200, json=RISK_RESULT)
        response = self.client.post(MEASUREMENTS_PATH, json=BATCH, headers=self.auth())
        self.assertEqual((response.status_code, response.json()), (200, RISK_RESULT))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_get_settings_is_relayed_without_cache(self) -> None:
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=SETTINGS)
        response = self.client.get(
            SETTINGS_PATH, headers=self.auth(**{"X-Correlation-ID": correlation})
        )
        self.assertEqual((response.status_code, response.json()), (200, SETTINGS))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", SETTINGS_PATH))
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)

    def test_put_forwards_the_body_as_is(self) -> None:
        custom = {**SETTINGS, "max_heart_rate_alert_bpm": 150, "source": "CUSTOM"}
        custom["updated_at"] = "2026-10-24T12:00:00Z"
        self.reply = httpx.Response(200, json=custom)
        body = {"max_heart_rate_alert_bpm": 150, "extra": True}
        response = self.client.put(SETTINGS_PATH, json=body, headers=self.auth())
        self.assertEqual((response.status_code, response.json()), (200, custom))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("PUT", SETTINGS_PATH))
        self.assertEqual(json.loads(sent.content), body)

    def test_service_errors_pass_through(self) -> None:
        cases = [
            ("put", 422, error("validation_error", [{"field": "x", "issue": "out_of_range"}])),
            ("get", 503, error("upstream_unavailable")),
            ("put", 403, error("capability_required")),
        ]
        for method, status, body in cases:
            self.reply = httpx.Response(status, json=body)
            if method == "get":
                response = self.client.get(SETTINGS_PATH, headers=self.auth())
            else:
                response = self.client.put(SETTINGS_PATH, json={}, headers=self.auth())
            with self.subTest(method=method, status=status):
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_settings_require_the_realtime_capability(self) -> None:
        """CA5: sin `training.realtime.monitor`, `403 capability_required` sin llamar."""
        self.upstream.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        for response in (
            self.client.get(SETTINGS_PATH, headers=self.auth()),
            self.client.put(
                SETTINGS_PATH, json={"max_heart_rate_alert_bpm": 150}, headers=self.auth()
            ),
        ):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        organizer = self.client.get(SETTINGS_PATH, headers=self.auth("organizer"))
        self.assertEqual(organizer.json()["code"], "permission_required")
        self.assertEqual(self.client.get(SETTINGS_PATH).status_code, 401)
        self.assertEqual(self.requests, [])
        self.upstream.snapshot = {"subscription_status": "ACTIVE", "capabilities": [REALTIME]}
        self.reply = httpx.Response(200, json=SETTINGS)
        self.assertEqual(self.client.get(SETTINGS_PATH, headers=self.auth()).status_code, 200)

    def test_unexpected_status_becomes_unavailable(self) -> None:
        self.reply = httpx.Response(409, json=error("conflict"))
        response = self.client.get(SETTINGS_PATH, headers=self.auth())
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )


if __name__ == "__main__":
    unittest.main()
