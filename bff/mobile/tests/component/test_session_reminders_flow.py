"""Componente HU038 en móvil: BFF → Usuarios (sesión y JWKS) → Afiliación (capacidad
`nutrition.session.reminders` del plan `INTERMEDIATE`) → Entrenamientos →
`session_reminder_preference`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`); reutiliza el montaje de
`test_device_flow`. Las preferencias no emiten eventos, así que no interviene Pub/Sub. Recorrido
de ADR-045 por el canal: sin capacidad `403` → plan `INTERMEDIATE` → ruleset → preferencias por
defecto → cambio con `expected_version` → `409` con una versión obsoleta y `422` fuera de rango.
"""

import unittest
from typing import Any

import test_device_flow
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from training_management.infrastructure.persistence_write.models import (
    SessionReminderPreferenceRow,
)

RULES = "/v1/session-reminder-rules"
PREFERENCES = "/v1/me/session-reminder-preferences"
DISCLAIMER = "Recordatorio general: no es un diagnóstico médico ni prescribe cantidades."
_FLOW = test_device_flow.DeviceFlowTest


def body(hydration: int, nutrition: int, expected_version: int) -> dict[str, Any]:
    return {
        "hydration": {"enabled": True, "interval_minutes": hydration},
        "nutrition": {"enabled": False, "interval_minutes": nutrition},
        "expected_version": expected_version,
    }


class SessionRemindersFlowTest(unittest.TestCase):
    # Sin importar la clase: el descubrimiento volvería a ejecutar sus pruebas aquí.
    athlete = _FLOW.athlete
    upgrade_to_intermediate = _FLOW.upgrade_to_intermediate

    def setUp(self) -> None:
        _FLOW.setUp(self)  # type: ignore[arg-type]

    def test_rules_and_preferences_through_the_mobile_channel(self) -> None:
        """CA4-CA5 en el backend, con identidad, plan y persistencia reales."""
        athlete = self.athlete()
        auth = {"Authorization": athlete["Authorization"]}

        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(plan="FREE", method=method, path=path):
                denied = self.bff.request(method, path, json=body(20, 60, 0), headers=auth)
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(denied.json()["code"], "capability_required")

        self.upgrade_to_intermediate(athlete)
        rules = self.bff.get(RULES, headers=auth)
        self.assertEqual(rules.status_code, 200, rules.text)
        self.assertEqual(rules.json()["ruleset_version"], "session-reminders-2026-10-13.v1")
        self.assertEqual(
            [
                (rule["type"], rule["default_interval_minutes"], rule["min"], rule["max"])
                for rule in rules.json()["rules"]
            ],
            [("HYDRATION", 15, 10, 60), ("NUTRITION", 45, 30, 120)],
        )
        self.assertEqual({rule["disclaimer"] for rule in rules.json()["rules"]}, {DISCLAIMER})

        default = self.bff.get(PREFERENCES, headers=auth)
        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(default.headers["Cache-Control"], "no-store")
        self.assertEqual(
            default.json(),
            {
                "hydration": {"enabled": True, "interval_minutes": 15},
                "nutrition": {"enabled": True, "interval_minutes": 45},
                "version": 0,
            },
        )

        saved = self.bff.put(PREFERENCES, json=body(10, 120, 0), headers=auth)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["version"], 1)
        self.assertEqual(self.bff.get(PREFERENCES, headers=auth).json(), saved.json())

        stale = self.bff.put(PREFERENCES, json=body(30, 60, 0), headers=auth)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(
            (stale.json()["code"], stale.json()["details"]),
            ("version_conflict", [{"field": "expected_version", "issue": "1"}]),
        )
        invalid = self.bff.put(PREFERENCES, json=body(61, 29, 1), headers=auth)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertCountEqual(
            invalid.json()["details"],
            [
                {"field": "hydration.interval_minutes", "issue": "out_of_range"},
                {"field": "nutrition.interval_minutes", "issue": "out_of_range"},
            ],
        )

        with sessionmaker(self.engine)() as session:
            (row,) = session.scalars(select(SessionReminderPreferenceRow)).all()
        self.assertEqual(str(row.athlete_id), athlete["user_id"])
        self.assertEqual(
            (row.hydration_interval_minutes, row.nutrition_enabled, row.version), (10, False, 1)
        )

    def test_other_athlete_does_not_see_or_change_these_preferences(self) -> None:
        first, second = self.athlete(), self.athlete()
        for athlete in (first, second):
            self.upgrade_to_intermediate(athlete)
        put = self.bff.put(
            PREFERENCES, json=body(25, 50, 0), headers={"Authorization": first["Authorization"]}
        )
        self.assertEqual(put.status_code, 200, put.text)
        other = self.bff.get(PREFERENCES, headers={"Authorization": second["Authorization"]})
        self.assertEqual((other.status_code, other.json()["version"]), (200, 0))
        own = self.bff.get(PREFERENCES, headers={"Authorization": first["Authorization"]})
        self.assertEqual(own.json()["hydration"]["interval_minutes"], 25)


if __name__ == "__main__":
    unittest.main()
