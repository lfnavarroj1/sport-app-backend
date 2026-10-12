"""Contrato interno de capacidades (ADR-024) con el caso de uso real y datos en memoria."""

import unittest
import uuid

from fakes import (
    FixedClock,
    InMemoryAffiliationStore,
    InMemoryCapabilitiesReader,
    provision_with,
    user_registered,
)
from fastapi.testclient import TestClient

from affiliation_management.api.capabilities import build_capabilities_router
from affiliation_management.application.messages import parse_user_registered
from affiliation_management.application.queries.user_capabilities import GetUserCapabilities
from affiliation_management.domain.catalog import FREE_CAPABILITIES
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings


class CapabilitiesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAffiliationStore()
        self.clock = FixedClock()
        query = GetUserCapabilities(reader=InMemoryCapabilitiesReader(self.store), clock=self.clock)
        app = create_service_app(
            service_name="affiliation-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_capabilities_router(query)],
        )
        self.client = TestClient(app)

    def get(self, user_id: object) -> object:
        return self.client.get(f"/v1/subscriptions/by-user/{user_id}/capabilities")

    def test_active_free_subscription_returns_its_eleven_capabilities(self) -> None:
        incoming = parse_user_registered(user_registered())
        provision_with(self.store, self.clock)(incoming)

        response = self.get(incoming.user_id)

        body = response.json()  # type: ignore[attr-defined]
        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual(body["subscription_status"], "ACTIVE")
        self.assertEqual(body["plan_code"], "FREE")
        self.assertEqual(body["plan_version"], 1)
        self.assertEqual(body["catalog_version"], "plans-2026-10-11.v1")
        self.assertEqual(body["capabilities"], sorted(FREE_CAPABILITIES))
        self.assertNotIn("training.realtime.monitor", body["capabilities"])

    def test_user_without_subscription_is_none_and_gets_no_capabilities(self) -> None:
        """CA8 (lado Afiliación): nunca se concede FREE por suposición."""
        response = self.get(uuid.uuid4())

        body = response.json()  # type: ignore[attr-defined]
        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual(body["subscription_status"], "NONE")
        self.assertIsNone(body["plan_code"])
        self.assertEqual(body["capabilities"], [])

    def test_invalid_user_id_is_422(self) -> None:
        response = self.get("no-es-uuid")

        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
