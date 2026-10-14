"""Componente HU020 en web: BFF → Usuarios (sesión y JWKS) → Servicios → Afiliación
(capacidad médica) → PostgreSQL migrado (requiere `TEST_DATABASE_URL`).

Los servicios se crean y publican con las operaciones reales de HU015 y HU019 por el BFF.
El deportista `FREE` usa la Afiliación real con su alta gratuita; `PREMIUM` todavía no se
puede obtener (HU016), así que ese caso usa una Afiliación simulada que responde la
capacidad, igual que la caída de Afiliación.
"""

import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock as AffiliationClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from complementary_services.main import create_app as create_services
from complementary_services.settings import ComplementaryServicesSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS = "users_management.infrastructure.persistence_write:migrations"
SERVICES = "complementary_services.infrastructure.persistence:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-11.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
DAY = datetime.now(UTC) + timedelta(days=10)


def offer(name: str, category: str, *, later: bool = False) -> dict[str, Any]:
    """Con `later`, su único intervalo queda fuera del rango que consulta la prueba (publicar
    exige al menos uno futuro, HU019)."""
    day = (DAY + timedelta(days=20 if later else 0)).date().isoformat()
    return {
        "name": name,
        "description": "Servicio sintético para consultar su disponibilidad.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "85000.00", "currency": "COP", "billing_unit": "HOUR"},
        "availability": [
            {
                "starts_at": f"{day}T09:00:00-05:00",
                "ends_at": f"{day}T10:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 2,
            }
        ],
    }


class ServiceAvailabilityFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu020_flow_users"))
        services_url = self.enterContext(temporary_database("hu020_flow_services"))
        affiliation_url = self.enterContext(temporary_database("hu020_flow_affiliation"))
        for history, url in (
            (USERS, users_url),
            (SERVICES, services_url),
            (AFFILIATION, affiliation_url),
        ):
            upgrade(history, url)
        engine = create_engine(affiliation_url)
        self.addCleanup(engine.dispose)
        self.affiliation_sessions = sessionmaker(engine)
        self.services_url = services_url
        self.users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        self.affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=affiliation_url,  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
        self.bff = self.bff_with(self.affiliation)

    def bff_with(self, affiliation: httpx.Client) -> TestClient:
        services = self.enterContext(
            TestClient(
                create_services(
                    ComplementaryServicesSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.services_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                ),
                base_url="http://complementary-services",
            )
        )
        return self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=self.users,
                    affiliation_http=self.affiliation,
                    services_http=services,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

    def session_for(self, actor_type: str, *, free_plan: bool = False) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user_id"]
        if free_plan:
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(self.affiliation_sessions),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    def publish(
        self, provider: dict[str, str], body: dict[str, Any], *, publish: bool = True
    ) -> str:
        created = self.bff.post(
            "/v1/services", json=body, headers={**provider, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.assertEqual(created.status_code, 201, created.text)
        service_id: str = created.json()["service_id"]
        if publish:
            published = self.bff.post(
                f"/v1/services/{service_id}/publish",
                headers={**provider, "Idempotency-Key": str(uuid.uuid4())},
            )
            self.assertEqual(published.status_code, 200, published.text)
        return service_id

    def catalog(self, user: dict[str, str], query: str = "", bff: TestClient | None = None) -> Any:
        start = (DAY - timedelta(days=5)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (DAY + timedelta(days=5)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (bff or self.bff).get(
            f"/v1/services/availability?from={start}&to={end}{query}", headers=user
        )

    def simulated_affiliation(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.Client:
        return httpx.Client(base_url="http://affiliation", transport=httpx.MockTransport(handler))

    def test_free_athlete_sees_published_non_medical_services(self) -> None:
        """CA1, CA2 y CA6 de punta a punta con la Afiliación real (`FREE`)."""
        provider = self.session_for("service_provider")
        coaching = self.publish(provider, offer("Entrenamiento funcional", "COACHING"))
        self.publish(provider, offer("Valoración médica", "SPORTS_MEDICINE"))
        empty = self.publish(provider, offer("Transporte a la carrera", "TRANSPORT", later=True))
        self.publish(provider, offer("Borrador de nutrición", "NUTRITION"), publish=False)
        athlete = self.session_for("athlete", free_plan=True)

        available = self.catalog(athlete)
        everything = self.catalog(athlete, "&availability=all")
        medical_only = self.catalog(athlete, "&category=SPORTS_MEDICINE")

        self.assertEqual(available.status_code, 200, available.text)
        body = available.json()
        self.assertEqual([item["service_id"] for item in body["items"]], [coaching])
        self.assertEqual(body["restricted_categories"], ["SPORTS_MEDICINE"])
        [item] = body["items"]
        self.assertEqual(
            item["price"], {"amount": "85000.00", "currency": "COP", "billing_unit": "HOUR"}
        )
        self.assertTrue(item["availability"][0]["starts_at"].endswith("Z"))
        self.assertEqual(item["availability"][0]["time_zone"], "America/Bogota")
        self.assertEqual(
            [(i["service_id"], i["available"]) for i in everything.json()["items"]],
            [(coaching, True), (empty, False)],
        )
        self.assertEqual(medical_only.json()["items"], [])
        self.assertEqual(medical_only.json()["restricted_categories"], ["SPORTS_MEDICINE"])

    def test_premium_capability_and_unavailable_affiliation(self) -> None:
        """CA1 con la capacidad médica (Afiliación simulada); Afiliación caída → `503`."""
        provider = self.session_for("service_provider")
        self.publish(provider, offer("Valoración médica", "SPORTS_MEDICINE"))
        self.publish(provider, offer("Entrenamiento funcional", "COACHING"))
        athlete = self.session_for("athlete")

        premium = self.bff_with(
            self.simulated_affiliation(
                lambda _: httpx.Response(
                    200,
                    json={
                        "subscription_status": "ACTIVE",
                        "capabilities": ["services.sports_medicine.access"],
                    },
                )
            )
        )

        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("afiliación caída")

        unavailable = self.bff_with(self.simulated_affiliation(down))

        with_capability = self.catalog(athlete, bff=premium)
        failed = self.catalog(athlete, bff=unavailable)
        coaching_only = self.catalog(athlete, "&category=COACHING", bff=unavailable)
        provider_view = self.catalog(provider)

        self.assertEqual(
            [item["name"] for item in with_capability.json()["items"]],
            ["Entrenamiento funcional", "Valoración médica"],
        )
        self.assertEqual(with_capability.json()["restricted_categories"], [])
        self.assertEqual((failed.status_code, failed.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual(
            coaching_only.status_code, 200, "sin categoría médica no depende de Afiliación"
        )
        self.assertEqual(
            (provider_view.status_code, provider_view.json()["code"]), (403, "permission_required")
        )


if __name__ == "__main__":
    unittest.main()
