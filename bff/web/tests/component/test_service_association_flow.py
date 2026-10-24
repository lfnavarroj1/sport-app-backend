"""Componente HU017 en web: BFF → Usuarios (sesión y JWKS) → Afiliación (capacidad
`services.sports_medicine.access`) → Servicios → PostgreSQL migrado (requiere
`TEST_DATABASE_URL`).

Los servicios se crean y publican con las operaciones reales de HU015 y HU019 y el
deportista obtiene `PREMIUM` con el cambio de plan real de HU016, todo por el BFF. La caída
de Afiliación usa una Afiliación simulada.

Trazabilidad: CA1 `test_premium_athlete_associates_lists_and_dissociates`; CA2
`test_ineligible_services_and_missing_capability`; CA3 duplicado y clave repetida en
`test_premium_athlete_associates_lists_and_dissociates`; CA4 ausencia de datos de salud en
la respuesta y la tabla; CA5 desasociación repetida e historial con `status=ALL`.
"""

import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
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
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
DAY = datetime.now(UTC) + timedelta(days=10)
PATH = "/v1/me/service-associations"


def offer(name: str, category: str = "SPORTS_MEDICINE") -> dict[str, Any]:
    day = DAY.date().isoformat()
    return {
        "name": name,
        "description": "Servicio sintético para asociarlo desde el catálogo.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "150000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": f"{day}T09:00:00-05:00",
                "ends_at": f"{day}T10:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 2,
            }
        ],
    }


class ServiceAssociationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu017_flow_users"))
        services_url = self.enterContext(temporary_database("hu017_flow_services"))
        affiliation_url = self.enterContext(temporary_database("hu017_flow_affiliation"))
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
                        health_data_consent_version="health-test.v1",
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
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                ),
                base_url="http://affiliation-management",
            )
        )
        self.bff = self.bff_with(self.affiliation)

    def bff_with(self, affiliation: httpx.Client) -> TestClient:
        """BFF y servicio consultan la misma Afiliación (real o simulada)."""
        self.services = services = self.enterContext(
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
                    affiliation_http=affiliation,
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

    def simulated_affiliation(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.Client:
        return httpx.Client(base_url="http://affiliation", transport=httpx.MockTransport(handler))

    def premium(self, athlete: dict[str, str]) -> None:
        """HU016: cambio real de `FREE` a `PREMIUM` por el BFF."""
        changed = self.bff.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": "PREMIUM",
                "periodicity": "MONTHLY",
                "expected_subscription_version": 1,
            },
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(changed.status_code, 200, changed.text)

    def associate(
        self, athlete: dict[str, str], service_id: str, key: str | None = None
    ) -> httpx.Response:
        return self.bff.post(
            PATH,
            json={"service_id": service_id},
            headers={**athlete, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def test_premium_athlete_associates_lists_and_dissociates(self) -> None:
        """CA1, CA3, CA4 y CA5 de punta a punta con la Afiliación real (`PREMIUM`)."""
        provider = self.session_for("service_provider")
        service_id = self.publish(provider, offer("Valoración médica deportiva"))
        athlete = self.session_for("athlete", free_plan=True)
        self.premium(athlete)
        key = str(uuid.uuid4())

        created = self.associate(athlete, service_id, key)
        replayed = self.associate(athlete, service_id, key)
        duplicate = self.associate(athlete, service_id)
        association_id = created.json()["association_id"]
        active = self.bff.get(PATH, headers=athlete)
        provider_view = self.bff.get(PATH, headers=provider)
        removed = self.bff.delete(f"{PATH}/{association_id}", headers=athlete)
        removed_again = self.bff.delete(f"{PATH}/{association_id}", headers=athlete)
        after = self.bff.get(PATH, headers=athlete)
        history = self.bff.get(f"{PATH}?status=ALL", headers=athlete)

        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(
            (body["service_id"], body["status"], body["version"]), (service_id, "ACTIVE", 1)
        )
        self.assertTrue(body["associated_at"].endswith("Z"))
        self.assertEqual(
            body["service"],
            {
                "name": "Valoración médica deportiva",
                "category": "SPORTS_MEDICINE",
                "modality": "IN_PERSON",
                "fee": {"amount": "150000.00", "currency": "COP", "billing_unit": "SESSION"},
            },
        )
        self.assertEqual(created.headers["Location"], f"{PATH}/{association_id}")
        self.assertEqual((replayed.status_code, replayed.json()), (201, body))
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (duplicate.status_code, duplicate.json()["code"], duplicate.json()["details"]),
            (409, "association_exists", [{"field": "association_id", "issue": association_id}]),
        )
        self.assertEqual(
            [item["association_id"] for item in active.json()["items"]], [association_id]
        )
        self.assertEqual(provider_view.json()["code"], "permission_required")
        self.assertEqual((removed.status_code, removed_again.status_code), (204, 204))
        self.assertEqual(after.json(), {"items": [], "next_cursor": None})
        [ended] = history.json()["items"]
        self.assertEqual((ended["status"], ended["version"]), ("INACTIVE", 2))
        self.assertTrue(ended["ended_at"].endswith("Z"))
        # CA4: la relación no guarda ni devuelve datos de salud.
        columns = {
            column["name"]
            for column in inspect(create_engine(self.services_url)).get_columns(
                "service_association"
            )
        }
        self.assertEqual(
            columns,
            {
                "association_id",
                "service_id",
                "athlete_id",
                "status",
                "associated_at",
                "ended_at",
                "version",
            },
        )

    def test_ineligible_services_and_missing_capability(self) -> None:
        """CA2: borrador u otra categoría → `404`; `FREE` → `403 capability_required` en el BFF
        y también en el servicio si se le llama sin pasar por el borde."""
        provider = self.session_for("service_provider")
        medical = self.publish(provider, offer("Valoración médica deportiva"))
        draft = self.publish(provider, offer("Borrador médico"), publish=False)
        coaching = self.publish(provider, offer("Entrenamiento funcional", "COACHING"))
        free = self.session_for("athlete", free_plan=True)
        premium = self.session_for("athlete", free_plan=True)
        self.premium(premium)

        without_capability = self.associate(free, medical)
        listed_without = self.bff.get(PATH, headers=free)
        direct = self.services.post(
            PATH,
            json={"service_id": medical},
            headers={**free, "Idempotency-Key": str(uuid.uuid4())},
        )
        not_found = [
            self.associate(premium, service) for service in (draft, coaching, str(uuid.uuid4()))
        ]

        for response in (without_capability, listed_without, direct):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        for response in not_found:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (404, "service_not_found")
            )
        self.assertEqual(self.bff.get(f"{PATH}?status=ALL", headers=premium).json()["items"], [])

    def test_unavailable_affiliation(self) -> None:
        """Afiliación caída → `503 upstream_unavailable`, sin crear la asociación."""
        provider = self.session_for("service_provider")
        medical = self.publish(provider, offer("Valoración médica deportiva"))
        athlete = self.session_for("athlete")

        def down(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("afiliación caída")

        unavailable = self.bff_with(self.simulated_affiliation(down))
        failed = unavailable.post(
            PATH,
            json={"service_id": medical},
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )
        direct = self.services.post(
            PATH,
            json={"service_id": medical},
            headers={**athlete, "Idempotency-Key": str(uuid.uuid4())},
        )

        for response in (failed, direct):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
            )


if __name__ == "__main__":
    unittest.main()
