"""Componente HU007 (ADR-036): `bff-web` → Usuarios (sesión y JWKS) → Afiliación → PostgreSQL,
para los tres actores del canal web.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El alta gratuita se
dispara con el caso de uso de Afiliación sobre el mismo evento que publicaría Usuarios (HU001
cubre ese tramo) y el cambio de plan pasa por el BFF (HU016).
"""

import unittest
import uuid
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.models import (
    BillingCommitmentRow,
    SubscriptionHistoryRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS_HISTORY = "users_management.infrastructure.persistence_write:migrations"
AFFILIATION_HISTORY = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
PATH = "/v1/me/account-statement"


class AccountStatementFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu007_users"))
        affiliation_url = self.enterContext(temporary_database("hu007_affiliation"))
        upgrade(USERS_HISTORY, users_url)
        upgrade(AFFILIATION_HISTORY, affiliation_url)
        users = self.enterContext(
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
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=affiliation_url,  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                ),
                base_url="http://affiliation-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        engine = create_engine(affiliation_url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)
        self.provision = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(engine)),
            clock=SystemClock(),
        )

    def register_and_login(self, actor_type: str = "athlete") -> tuple[UUID, dict[str, str]]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        session = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(session.status_code, 200, session.text)
        return (
            UUID(registered.json()["user_id"]),
            {"Authorization": f"Bearer {session.json()['access_token']}"},
        )

    def provision_free(self, user_id: UUID, actor_type: str = "athlete") -> None:
        self.provision(
            RegisteredUser(
                event_id=uuid.uuid4(), user_id=user_id, actor_type=actor_type, correlation_id=None
            )
        )

    def change(self, auth: dict[str, str], plan: str, periodicity: str, version: int) -> None:
        response = self.bff.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": plan,
                "periodicity": periodicity,
                "expected_subscription_version": version,
            },
            headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def statement(self, auth: dict[str, str], **params: str) -> dict[str, Any]:
        response = self.bff.get(PATH, params=params, headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        document: dict[str, Any] = response.json()
        self.assertEqual(
            (document["payments_status"], document["balance"]), ("NOT_INTEGRATED", None)
        )
        return document

    def rows(self) -> dict[str, int]:
        with self.sessions() as session:
            return {
                model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
                for model in (SubscriptionRow, BillingCommitmentRow, SubscriptionHistoryRow)
            }

    def test_athlete_from_provisioning_to_paid_plan_and_back_to_free(self) -> None:
        """CA1, CA2, CA4 y CA5 por el BFF: `PROVISIONING`, `FREE`, plan pago con su compromiso
        `PENDING_EXTERNAL` y vuelta a `FREE` con el compromiso `WAIVED`; movimientos paginados."""
        user_id, auth = self.register_and_login()

        provisioning = self.statement(auth)
        self.assertEqual(provisioning["statement_status"], "PROVISIONING")
        self.assertEqual(
            (provisioning["subscription"], provisioning["commitments"], provisioning["movements"]),
            (None, [], []),
        )

        self.provision_free(user_id)
        free = self.statement(auth)
        self.assertEqual(free["statement_status"], "ACTIVE")
        self.assertEqual(
            (free["subscription"]["plan_code"], free["subscription"]["price"]), ("FREE", "0.00")
        )
        self.assertEqual([m["movement_type"] for m in free["movements"]], ["FREE_ACTIVATED"])

        self.change(auth, "INTERMEDIATE", "ANNUAL", 1)
        paid = self.statement(auth)
        self.assertEqual(
            (paid["subscription"]["plan_code"], paid["subscription"]["periodicity"]),
            ("INTERMEDIATE", "ANNUAL"),
        )
        [commitment] = paid["commitments"]
        self.assertEqual(
            (commitment["amount"], commitment["currency"], commitment["status"]),
            ("28800.00", "COP", "PENDING_EXTERNAL"),
        )
        self.assertEqual(commitment["period_end"], paid["subscription"]["period_end"])
        self.assertTrue(commitment["due_at"].endswith("Z"))

        self.change(auth, "FREE", "NONE", 2)
        before = self.rows()
        first = self.statement(auth, limit="2")
        second = self.statement(auth, limit="2", cursor=first["next_cursor"])
        third = self.statement(auth, limit="2", cursor=second["next_cursor"])

        self.assertEqual(
            [(c["commitment_id"], c["status"]) for c in first["commitments"]],
            [(commitment["commitment_id"], "WAIVED")],
        )
        movements = first["movements"] + second["movements"] + third["movements"]
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(
            sorted(m["movement_type"] for m in movements),
            sorted(
                [
                    "FREE_ACTIVATED",
                    "PLAN_CHANGED",
                    "COMMITMENT_CREATED",
                    "RETURNED_TO_FREE",
                    "COMMITMENT_WAIVED",
                ]
            ),
        )
        self.assertEqual(len({m["movement_id"] for m in movements}), 5)
        self.assertEqual(movements[-1]["movement_type"], "FREE_ACTIVATED")
        # La consulta no produce efectos.
        self.assertEqual(self.rows(), before)

    def test_organizer_and_professional_receive_not_applicable(self) -> None:
        """CA2 por el BFF: `NOT_APPLICABLE` con listas vacías, no un error técnico."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                user_id, auth = self.register_and_login(actor)
                # El consumidor no aprovisiona a otros actores (HU001); nada que listar.
                self.provision_free(user_id, actor)
                document = self.statement(auth)
                self.assertEqual(
                    (
                        document["statement_status"],
                        document["subscription"],
                        document["commitments"],
                        document["movements"],
                        document["next_cursor"],
                    ),
                    ("NOT_APPLICABLE", None, [], [], None),
                )

    def test_each_user_only_reads_their_own_account(self) -> None:
        """CA3: dos deportistas con historial distinto; ningún parámetro cambia la cuenta."""
        first_id, first_auth = self.register_and_login()
        second_id, second_auth = self.register_and_login()
        self.provision_free(first_id)
        self.provision_free(second_id)
        self.change(second_auth, "PREMIUM", "MONTHLY", 1)

        first = self.statement(first_auth, user_id=str(second_id))
        second = self.statement(second_auth)

        self.assertEqual(first["subscription"]["plan_code"], "FREE")
        self.assertEqual(first["commitments"], [])
        self.assertEqual(len(first["movements"]), 1)
        self.assertEqual(second["subscription"]["plan_code"], "PREMIUM")
        self.assertEqual(len(second["commitments"]), 1)
        own = {m["subscription_id"] for m in first["movements"]}
        self.assertNotIn(second["subscription"]["subscription_id"], own)

    def test_security_and_validation_errors_through_the_bff(self) -> None:
        """Seguridad: sin token `401`; `limit`/`cursor` inválidos → `422` del servicio."""
        missing = self.bff.get(PATH)
        self.assertEqual(
            (missing.status_code, missing.json()["code"]), (401, "invalid_access_token")
        )

        _, auth = self.register_and_login("organizer")
        for params, field, issue in (
            ({"limit": "0"}, "query.limit", "out_of_range"),
            ({"limit": "101"}, "query.limit", "out_of_range"),
            ({"cursor": "no-es-un-cursor"}, "query.cursor", "invalid_cursor"),
        ):
            with self.subTest(params=params):
                response: httpx.Response = self.bff.get(PATH, params=params, headers=auth)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])


if __name__ == "__main__":
    unittest.main()
