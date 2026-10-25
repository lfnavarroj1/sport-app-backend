"""HU038 en el servicio con dobles: forma del ruleset, rangos y versión de las preferencias
(CA5), habilitación por tipo (CA4), autorización y errores de `/v1/session-reminder-rules` y
`/v1/me/session-reminder-preferences`."""

import logging
import unittest
from collections.abc import Callable
from datetime import timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from fakes import NOW, FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.session_reminders import build_session_reminders_router
from training_management.application.commands.session_reminder_preferences import (
    UpdateSessionReminderPreferences,
)
from training_management.application.errors import (
    CapabilitiesTimeout,
    SessionReminderPreferencesConflict,
)
from training_management.application.queries.session_reminders import (
    GetSessionReminderPreferences,
    GetSessionReminderRules,
)
from training_management.domain.reminders.model import (
    DISCLAIMER,
    RULESET,
    RULESET_VERSION,
    InvalidReminderPreferences,
    PreferencesChange,
    ReminderPreferences,
    ReminderPreferencesVersionConflict,
    ReminderSetting,
    ReminderType,
    validate_preferences,
)

CAPABILITY = "nutrition.session.reminders"
RULES = "/v1/session-reminder-rules"
PREFERENCES = "/v1/me/session-reminder-preferences"


def body(
    hydration: tuple[bool, int] = (True, 20),
    nutrition: tuple[bool, int] = (False, 60),
    expected_version: int = 0,
) -> dict[str, Any]:
    return {
        "hydration": {"enabled": hydration[0], "interval_minutes": hydration[1]},
        "nutrition": {"enabled": nutrition[0], "interval_minutes": nutrition[1]},
        "expected_version": expected_version,
    }


def violations(raw: object) -> set[tuple[str | None, str]]:
    with unittest.TestCase().assertRaises(InvalidReminderPreferences) as caught:
        validate_preferences(raw)
    return set(caught.exception.violations)


class InMemoryPreferencesUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma clave primaria que PostgreSQL."""

    def __init__(
        self,
        store: dict[UUID, ReminderPreferences],
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._added: list[ReminderPreferences] = []
        self._updated: list[ReminderPreferences] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._added, self._updated = [], []

    def get_preferences(
        self, athlete_id: UUID, *, for_update: bool = False
    ) -> ReminderPreferences | None:
        return self.store.get(athlete_id)

    def add_preferences(self, preferences: ReminderPreferences) -> None:
        self._added.append(preferences)

    def update_preferences(self, preferences: ReminderPreferences) -> None:
        self._updated.append(preferences)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        if any(item.athlete_id in self.store for item in self._added):
            raise SessionReminderPreferencesConflict()
        for item in [*self._added, *self._updated]:
            self.store[item.athlete_id] = item


class RulesetTest(unittest.TestCase):
    def test_ruleset_has_the_approved_version_rules_and_texts(self) -> None:
        self.assertEqual(RULESET.ruleset_version, "session-reminders-2026-10-13.v1")
        hydration = RULESET.rule(ReminderType.HYDRATION)
        nutrition = RULESET.rule(ReminderType.NUTRITION)
        self.assertEqual(
            (hydration.default_interval_minutes, hydration.min, hydration.max), (15, 10, 60)
        )
        self.assertEqual(hydration.applies_after_minutes, 0)
        self.assertEqual(hydration.message, "Hidrátate: bebe agua o bebida isotónica")
        self.assertEqual(
            (nutrition.default_interval_minutes, nutrition.min, nutrition.max), (45, 30, 120)
        )
        self.assertEqual(nutrition.applies_after_minutes, 60)
        self.assertEqual(nutrition.message, "Consume un alimento ligero según tu plan")
        self.assertEqual(
            DISCLAIMER,
            "Recordatorio general: no es un diagnóstico médico ni prescribe cantidades.",
        )
        self.assertEqual({rule.disclaimer for rule in RULESET.rules}, {DISCLAIMER})
        self.assertEqual([rule.type for rule in RULESET.rules], list(ReminderType))

    def test_defaults_are_within_their_own_range(self) -> None:
        for rule in RULESET.rules:
            with self.subTest(rule=rule.type):
                self.assertLessEqual(rule.min, rule.default_interval_minutes)
                self.assertLessEqual(rule.default_interval_minutes, rule.max)


class ValidationTest(unittest.TestCase):
    def test_valid_body_at_the_range_limits(self) -> None:
        for hydration, nutrition in ((10, 30), (60, 120)):
            with self.subTest(hydration=hydration, nutrition=nutrition):
                change = validate_preferences(
                    body((True, hydration), (False, nutrition), expected_version=3)
                )
                self.assertEqual(
                    change,
                    PreferencesChange(
                        ReminderSetting(True, hydration), ReminderSetting(False, nutrition), 3
                    ),
                )

    def test_out_of_range_intervals(self) -> None:
        for hydration, nutrition in ((9, 29), (61, 121)):
            with self.subTest(hydration=hydration, nutrition=nutrition):
                self.assertEqual(
                    violations(body((True, hydration), (True, nutrition))),
                    {
                        ("hydration.interval_minutes", "out_of_range"),
                        ("nutrition.interval_minutes", "out_of_range"),
                    },
                )

    def test_missing_body_and_non_object(self) -> None:
        self.assertEqual(violations(None), {(None, "required")})
        self.assertEqual(violations([1]), {(None, "unknown_value")})

    def test_missing_and_wrong_types(self) -> None:
        self.assertEqual(
            violations({}),
            {
                ("hydration", "required"),
                ("nutrition", "required"),
                ("expected_version", "required"),
            },
        )
        self.assertEqual(
            violations(
                {
                    "hydration": {"enabled": "yes", "interval_minutes": 20.5},
                    "nutrition": "on",
                    "expected_version": True,
                }
            ),
            {
                ("hydration.enabled", "unknown_value"),
                ("hydration.interval_minutes", "unknown_value"),
                ("nutrition", "unknown_value"),
                ("expected_version", "unknown_value"),
            },
        )
        self.assertEqual(
            violations({**body(), "hydration": {"enabled": True, "interval_minutes": True}}),
            {("hydration.interval_minutes", "unknown_value")},
        )
        self.assertEqual(
            violations({**body(), "nutrition": {}}),
            {("nutrition.enabled", "required"), ("nutrition.interval_minutes", "required")},
        )

    def test_extra_fields_and_negative_version(self) -> None:
        raw = body(expected_version=-1)
        raw["athlete_id"] = str(uuid4())
        raw["hydration"]["amount_ml"] = 250
        self.assertEqual(
            violations(raw),
            {
                ("athlete_id", "field_not_allowed"),
                ("hydration.amount_ml", "field_not_allowed"),
                ("expected_version", "out_of_range"),
            },
        )


class PreferencesModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete_id = uuid4()

    def change(self, expected_version: int, hydration: int = 20) -> PreferencesChange:
        return PreferencesChange(
            ReminderSetting(True, hydration), ReminderSetting(False, 60), expected_version
        )

    def test_default_uses_the_ruleset_both_enabled_version_zero(self) -> None:
        default = ReminderPreferences.default(self.athlete_id)
        self.assertEqual(
            (default.hydration, default.nutrition, default.version),
            (ReminderSetting(True, 15), ReminderSetting(True, 45), 0),
        )
        self.assertFalse(default.stored)

    def test_versions_advance_and_same_values_do_not_change(self) -> None:
        first = ReminderPreferences.default(self.athlete_id).apply(self.change(0), NOW)
        self.assertEqual((first.version, first.created_at, first.updated_at), (1, NOW, NOW))
        later = NOW + timedelta(minutes=5)
        self.assertIs(first.apply(self.change(1), later), first)
        second = first.apply(self.change(1, hydration=30), later)
        self.assertEqual((second.version, second.created_at, second.updated_at), (2, NOW, later))

    def test_saving_the_defaults_explicitly_stores_version_one(self) -> None:
        default = ReminderPreferences.default(self.athlete_id)
        change = PreferencesChange(default.hydration, default.nutrition, 0)
        self.assertEqual(default.apply(change, NOW).version, 1)

    def test_stale_version_conflicts_with_the_current_one(self) -> None:
        stored = ReminderPreferences.default(self.athlete_id).apply(self.change(0), NOW)
        for expected in (0, 2):
            with self.subTest(expected=expected):
                with self.assertRaises(ReminderPreferencesVersionConflict) as caught:
                    stored.apply(self.change(expected), NOW)
                self.assertEqual(caught.exception.current_version, 1)


class SessionRemindersApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete_id = uuid4()
        self.store: dict[UUID, ReminderPreferences] = {}
        self.capabilities = FakeCapabilities({self.athlete_id: {CAPABILITY}})
        self.clock = FixedClock()
        self.before_commit: Callable[[], None] | None = None
        self.update = UpdateSessionReminderPreferences(
            unit_of_work=self.unit_of_work, capabilities=self.capabilities, clock=self.clock
        )
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_session_reminders_router(
                    verify_token=VERIFIER.verify,
                    get_rules=GetSessionReminderRules(capabilities=self.capabilities),
                    get_preferences=GetSessionReminderPreferences(
                        unit_of_work=self.unit_of_work, capabilities=self.capabilities
                    ),
                    update_preferences=self.update,
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def unit_of_work(self) -> InMemoryPreferencesUnitOfWork:
        hook, self.before_commit = self.before_commit, None
        return InMemoryPreferencesUnitOfWork(self.store, hook)

    def put(self, payload: Any, headers: dict[str, str] | None = None) -> Any:
        return self.client.put(PREFERENCES, json=payload, headers=headers or self.auth)

    def test_rules_return_the_versioned_ruleset(self) -> None:
        response = self.client.get(RULES, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ruleset_version"], RULESET_VERSION)
        self.assertEqual(
            document["rules"],
            [
                {
                    "type": "HYDRATION",
                    "default_interval_minutes": 15,
                    "min": 10,
                    "max": 60,
                    "applies_after_minutes": 0,
                    "message": "Hidrátate: bebe agua o bebida isotónica",
                    "disclaimer": DISCLAIMER,
                },
                {
                    "type": "NUTRITION",
                    "default_interval_minutes": 45,
                    "min": 30,
                    "max": 120,
                    "applies_after_minutes": 60,
                    "message": "Consume un alimento ligero según tu plan",
                    "disclaimer": DISCLAIMER,
                },
            ],
        )
        self.assertEqual(self.capabilities.calls, [(self.athlete_id, CAPABILITY)])

    def test_preferences_default_then_update_and_read_back(self) -> None:
        default = self.client.get(PREFERENCES, headers=self.auth)
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
        updated = self.put(body((True, 10), (False, 120)))
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.headers["Cache-Control"], "no-store")
        expected = {
            "hydration": {"enabled": True, "interval_minutes": 10},
            "nutrition": {"enabled": False, "interval_minutes": 120},
            "version": 1,
        }
        self.assertEqual(updated.json(), expected)
        self.assertEqual(self.client.get(PREFERENCES, headers=self.auth).json(), expected)
        again = self.put(body((True, 10), (False, 120), expected_version=1))
        self.assertEqual((again.status_code, again.json()["version"]), (200, 1))

    def test_stale_version_is_409_with_the_current_version(self) -> None:
        self.assertEqual(self.put(body()).status_code, 200)
        stale = self.put(body((True, 30)))
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "version_conflict")
        self.assertEqual(stale.json()["details"], [{"field": "expected_version", "issue": "1"}])
        self.assertEqual(self.store[self.athlete_id].hydration.interval_minutes, 20)

    def test_concurrent_first_save_retries_and_ends_in_version_conflict(self) -> None:
        def other_request_creates_first() -> None:
            other = ReminderPreferences.default(self.athlete_id).apply(
                PreferencesChange(ReminderSetting(False, 60), ReminderSetting(True, 30), 0), NOW
            )
            self.store[self.athlete_id] = other

        self.before_commit = other_request_creates_first
        response = self.put(body())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["details"], [{"field": "expected_version", "issue": "1"}])
        self.assertEqual(self.store[self.athlete_id].hydration, ReminderSetting(False, 60))

    def test_out_of_range_is_422_validation_error(self) -> None:
        response = self.put(body((True, 5), (True, 200)))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertCountEqual(
            response.json()["details"],
            [
                {"field": "hydration.interval_minutes", "issue": "out_of_range"},
                {"field": "nutrition.interval_minutes", "issue": "out_of_range"},
            ],
        )
        empty = self.client.put(PREFERENCES, headers=self.auth)
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["details"], [{"field": None, "issue": "required"}])
        self.assertEqual(self.store, {})

    def test_without_capability_is_403_capability_required(self) -> None:
        self.capabilities.granted = {}
        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=body(), headers=self.auth)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "capability_required")
        self.assertEqual(self.store, {})

    def test_other_actor_is_403_permission_required_without_checking_the_plan(self) -> None:
        auth = {"Authorization": f"Bearer {token(uuid4(), 'organizer')}"}
        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=body(), headers=auth)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.capabilities.calls, [])

    def test_missing_token_is_401(self) -> None:
        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=body())
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")

    def test_affiliation_down_or_slow_is_503_or_504(self) -> None:
        self.capabilities.unavailable = True
        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=body(), headers=self.auth)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["code"], "upstream_unavailable")

        class SlowCapabilities(FakeCapabilities):
            def has_capability(self, user_id: UUID, capability: str) -> bool:
                raise CapabilitiesTimeout()

        slow = UpdateSessionReminderPreferences(
            unit_of_work=self.unit_of_work, capabilities=SlowCapabilities(), clock=self.clock
        )
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_session_reminders_router(
                    verify_token=VERIFIER.verify,
                    get_rules=GetSessionReminderRules(capabilities=SlowCapabilities()),
                    get_preferences=GetSessionReminderPreferences(
                        unit_of_work=self.unit_of_work, capabilities=SlowCapabilities()
                    ),
                    update_preferences=slow,
                )
            ],
        )
        client = TestClient(app)
        for method, path in (("GET", RULES), ("GET", PREFERENCES), ("PUT", PREFERENCES)):
            with self.subTest(method=method, path=path, slow=True):
                response = client.request(method, path, json=body(), headers=self.auth)
                self.assertEqual(response.status_code, 504, response.text)
                self.assertEqual(response.json()["code"], "upstream_timeout")

    def test_audit_has_only_result_and_version_never_the_values(self) -> None:
        with self.assertLogs("sportapp.audit", level=logging.INFO) as captured:
            self.put(body((True, 37), (True, 97)))
            self.put(body((True, 37), (True, 97)))
        fields = [record.__dict__["fields"] for record in captured.records]
        self.assertEqual(
            [
                (item["result"], item.get("preferences_version"), item.get("reason"))
                for item in fields
            ],
            [("updated", 1, None), ("rejected", None, "version_conflict")],
        )
        # Solo claves sin contenido (comparar claves evita coincidencias casuales con el UUID).
        allowed = {"athlete_id", "result", "preferences_version", "reason"}
        for item in fields:
            self.assertLessEqual(set(item), allowed)


if __name__ == "__main__":
    unittest.main()
