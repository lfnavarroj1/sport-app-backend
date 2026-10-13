"""API de HU032 en el servicio: contrato de ADR-029, autenticación, errores y consulta activa."""

import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fakes import FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from session_fakes import (
    InMemoryActiveSessionReader,
    InMemorySessionStore,
    InMemorySessionUnitOfWork,
    start_body,
)
from test_api import SIGNER, VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import JwksUnavailable
from training_management.api.training_sessions import build_training_sessions_router
from training_management.application.commands.training_sessions import (
    FinishTrainingSession,
    StartTrainingSession,
)
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.ports import ActiveSessionView
from training_management.application.queries.active_session import GetActiveTrainingSession


class SlowAffiliation(FakeCapabilities):
    def has_capability(self, user_id: Any, capability: str) -> bool:
        raise CapabilitiesTimeout()


class TrainingSessionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySessionStore()
        self.active: dict[Any, ActiveSessionView] = {}
        self.athlete_id = uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {"training.session.record"}})
        self.jwks_down = False
        self.client = self.client_with(self.capabilities)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        clock = FixedClock()
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_training_sessions_router(
                    verify_token=verify,
                    start=StartTrainingSession(
                        unit_of_work=lambda: InMemorySessionUnitOfWork(self.store),
                        capabilities=capabilities,
                        clock=clock,
                    ),
                    finish=FinishTrainingSession(
                        unit_of_work=lambda: InMemorySessionUnitOfWork(self.store), clock=clock
                    ),
                    get_active=GetActiveTrainingSession(
                        reader=InMemoryActiveSessionReader(self.active)
                    ),
                )
            ],
        )
        return TestClient(app)

    def start(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post("/v1/training-sessions", json=body, headers=sent)

    def finish(self, session_id: str, body: Any, key: str | None = None) -> Any:
        sent = dict(self.auth)
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(
            f"/v1/training-sessions/{session_id}/finish", json=body, headers=sent
        )

    def test_start_and_finish_match_the_adr_contract(self) -> None:
        """CA1 y CA2 por HTTP, con reintentos idempotentes (CA4)."""
        body, key = start_body(), str(uuid4())
        started = self.start(body, key)
        replay = self.start(body, key)

        self.assertEqual(started.status_code, 201, started.text)
        session = started.json()
        self.assertEqual(
            started.headers["Location"], f"/v1/training-sessions/{session['session_id']}"
        )
        self.assertEqual(
            {name: session[name] for name in ("status", "version", "ended_at", "duration_seconds")},
            {"status": "ACTIVE", "version": 1, "ended_at": None, "duration_seconds": None},
        )
        self.assertEqual(session["athlete_id"], str(self.athlete_id))
        self.assertEqual(session["started_at"], "2026-10-12T11:30:00Z")
        self.assertEqual((replay.status_code, replay.json()), (201, session))
        self.assertEqual(replay.headers["Idempotent-Replayed"], "true")

        finish_key = str(uuid4())
        finished = self.finish(body["session_id"], {"ended_at": "2026-10-12T11:45:00Z"}, finish_key)
        again = self.finish(body["session_id"], {"ended_at": "2026-10-12T11:45:00Z"}, finish_key)

        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(
            {name: finished.json()[name] for name in ("status", "version", "duration_seconds")},
            {"status": "FINISHED", "version": 2, "duration_seconds": 900},
        )
        self.assertEqual(finished.json()["ended_at"], "2026-10-12T11:45:00Z")
        self.assertNotIn("Idempotent-Replayed", finished.headers)
        self.assertEqual(again.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.outbox), 2)

    def test_validation_errors_use_the_adr_catalog(self) -> None:
        body = start_body(athlete_id=str(uuid4()), started_at="2026-10-12T11:30:00+00:00")

        response = self.start(body, str(uuid4()))
        missing_key = self.start(start_body(sport="yoga"))

        self.assertEqual(response.status_code, 422)
        self.assertCountEqual(
            response.json()["details"],
            [
                {"field": "athlete_id", "issue": "field_not_allowed"},
                {"field": "started_at", "issue": "invalid_datetime"},
            ],
        )
        self.assertCountEqual(
            missing_key.json()["details"],
            [
                {"field": "Idempotency-Key", "issue": "required"},
                {"field": "sport", "issue": "unknown_value"},
            ],
        )
        bad_key = self.finish(str(uuid4()), {"ended_at": "2026-10-12T11:45:00Z"}, "x")
        self.assertEqual(
            bad_key.json()["details"], [{"field": "Idempotency-Key", "issue": "invalid_uuid"}]
        )
        self.assertEqual((self.store.sessions, self.store.outbox), ({}, []))

    def test_conflicts_and_not_found(self) -> None:
        """CA3, CA4 y CA5."""
        body = start_body()
        self.start(body, str(uuid4()))
        cases = [
            (self.start(start_body(), str(uuid4())), 409, "active_session_exists"),
            (
                self.start({**body, "sport": "cycling"}, str(uuid4())),
                409,
                "session_id_reused",
            ),
            (
                self.finish(str(uuid4()), {"ended_at": "2026-10-12T11:45:00Z"}, str(uuid4())),
                404,
                "training_session_not_found",
            ),
            (
                self.start(start_body(plan_id=str(uuid4())), str(uuid4())),
                404,
                "training_plan_not_found",
            ),
        ]
        for response, status, code in cases:
            with self.subTest(code=code):
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))

        self.finish(body["session_id"], {"ended_at": "2026-10-12T11:45:00Z"}, str(uuid4()))
        late = self.finish(body["session_id"], {"ended_at": "2026-10-12T11:50:00Z"}, str(uuid4()))
        self.assertEqual(
            (late.status_code, late.json()["code"]), (409, "training_session_already_finished")
        )

    def test_authentication_actor_and_capability(self) -> None:
        anonymous = self.client.post("/v1/training-sessions", json=start_body())
        provider = self.start(
            start_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), 'service_provider')}"
        )
        no_capability = self.start(
            start_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), 'athlete')}"
        )
        self.capabilities.unavailable = True
        down = self.start(start_body(), str(uuid4()))
        self.client = self.client_with(SlowAffiliation())
        slow = self.start(start_body(), str(uuid4()))
        self.jwks_down = True
        with self.assertLogs("sportapp.training", level="WARNING"):
            jwks = self.start(start_body(), str(uuid4()))

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (no_capability.status_code, no_capability.json()["code"]),
            (403, "capability_required"),
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(jwks.status_code, 503)
        self.assertEqual(self.store.sessions, {})
        self.assertIsNotNone(SIGNER)

    def test_active_session_query(self) -> None:
        empty = self.client.get("/v1/training-sessions/active", headers=self.auth)
        session_id = uuid4()
        self.active[session_id] = ActiveSessionView(
            session_id=session_id,
            athlete_id=self.athlete_id,
            plan_id=None,
            sport="swimming",
            started_at=datetime(2026, 10, 12, 11, 0, tzinfo=UTC),
            version=1,
        )
        self.active[uuid4()] = ActiveSessionView(
            session_id=uuid4(),
            athlete_id=uuid4(),
            plan_id=None,
            sport="running",
            started_at=datetime(2026, 10, 12, 11, 50, tzinfo=UTC),
            version=1,
        )
        found = self.client.get("/v1/training-sessions/active", headers=self.auth)
        provider = self.client.get(
            "/v1/training-sessions/active",
            headers={"Authorization": f"Bearer {token(uuid4(), 'service_provider')}"},
        )

        self.assertEqual(empty.status_code, 204)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(
            found.json(),
            {
                "session_id": str(session_id),
                "athlete_id": str(self.athlete_id),
                "plan_id": None,
                "sport": "swimming",
                "started_at": "2026-10-12T11:00:00Z",
                "status": "ACTIVE",
                "version": 1,
            },
        )
        self.assertEqual(provider.status_code, 403)


if __name__ == "__main__":
    unittest.main()
