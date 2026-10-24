"""Componente HU002 en web: BFF → Usuarios → outbox → Pub/Sub → proyector → `user_directory`.

Servicios reales (Usuarios y BFF) con PostgreSQL de escritura y de lectura migrados, el
publicador `usuarios-outbox-publisher` y el manejador de `usuarios-read-projector` contra el
emulador de Pub/Sub (base `ProfileFlowCase` de HU005, sin modificarla). Requiere
`TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`. Comprueba también que la sesión vigente sigue
renovándose tras cambiar el correo, que el siguiente inicio de sesión usa el correo nuevo y
que la suscripción del alta gratuita de Afiliación no recibe `usuario.actualizado.v1` (R-19).
"""

import json
import uuid
from typing import Any

from sqlalchemy import func, select
from test_profile_flow import PASSWORD, ProfileFlowCase

from affiliation_management.settings import FREE_PLAN_SUBSCRIPTION
from users_management.infrastructure.persistence_write.models import OutboxEventRow

COOKIE = "__Secure-sportapp_refresh"
NEW_NAME, NEW_EMAIL = "Organizadora Renombrada", "organizadora.nueva.hu002@example.com"


class AccountFlowTest(ProfileFlowCase):
    def put_account(
        self, session: dict[str, str], body: dict[str, Any], key: str | None = None
    ) -> Any:
        return self.bff.put(
            "/v1/me/account",
            json=body,
            headers={
                "Authorization": session["authorization"],
                "Idempotency-Key": key or str(uuid.uuid4()),
            },
        )

    def account(self, session: dict[str, str]) -> Any:
        return self.bff.get("/v1/me/account", headers={"Authorization": session["authorization"]})

    def updated_events(self) -> int:
        with self.write_engine.connect() as connection:
            return (
                connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(OutboxEventRow.event_type == "usuario.actualizado.v1")
                )
                or 0
            )

    def test_actor_edits_name_and_email_and_keeps_the_session(self) -> None:
        """CA1, CA5 y CA6 de punta a punta, para un organizador (no deportista)."""
        organizer = self.session_for("organizer", "organizadora.hu002@example.com")
        cookie = self.bff.cookies.get(COOKIE)
        self.assertIsNotNone(cookie)
        self.assertEqual(self.publish_outbox(), 2)  # Registro del deportista y del organizador.
        self.assertTrue(self.project_until(lambda: self.account(organizer).status_code == 200))
        before = self.account(organizer).json()
        self.assertEqual((before["version"], before["actor_type"]), (1, "organizer"))

        key = str(uuid.uuid4())
        body = {"expected_version": 1, "full_name": f"  {NEW_NAME} ", "email": NEW_EMAIL.upper()}
        updated = self.put_account(organizer, body, key)
        replayed = self.put_account(organizer, body, key)

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.headers["cache-control"], "no-store")
        confirmed = updated.json()
        self.assertEqual(
            {
                k: confirmed[k]
                for k in ("user_id", "full_name", "email", "actor_type", "status", "version")
            },
            {
                "user_id": organizer["user_id"],
                "full_name": NEW_NAME,
                "email": NEW_EMAIL,
                "actor_type": "organizer",
                "status": "active",
                "version": 2,
            },
        )
        self.assertEqual(replayed.json(), confirmed)
        self.assertEqual(replayed.headers["idempotent-replayed"], "true")
        self.assertEqual(self.updated_events(), 1, "una sola publicación lógica (CA5)")

        # CA6: la sesión vigente sigue renovándose con la misma cookie.
        renewed = self.bff.post("/v1/sessions/refresh")
        self.assertEqual(renewed.status_code, 200, renewed.text)

        self.assertEqual(self.publish_outbox(), 1)
        projected = self.project_until(lambda: self.account(organizer).json().get("version") == 2)
        self.assertTrue(projected, "el proyector no aplicó usuario.actualizado.v1")
        view = self.account(organizer).json()
        self.assertEqual((view["full_name"], view["email"]), (NEW_NAME, NEW_EMAIL))
        renewed_session = {"authorization": f"Bearer {renewed.json()['access_token']}"}
        self.assertEqual(self.account(renewed_session).json()["email"], NEW_EMAIL)

        # CA6: el siguiente inicio de sesión usa el correo nuevo.
        old = self.bff.post(
            "/v1/sessions", json={"email": "organizadora.hu002@example.com", "password": PASSWORD}
        )
        new = self.bff.post("/v1/sessions", json={"email": NEW_EMAIL, "password": PASSWORD})
        self.assertEqual((old.status_code, new.status_code), (401, 200), new.text)

        free_plan = self.subscriber.pull(
            request={
                "subscription": self.subscriber.subscription_path(
                    self.project, FREE_PLAN_SUBSCRIPTION
                ),
                "max_messages": 10,
            },
            timeout=30,
        ).received_messages
        self.assertEqual(
            {json.loads(item.message.data)["event_type"] for item in free_plan},
            {"usuario.registrado.v1"},
        )
        logged = self.capture.text()
        for value in (NEW_NAME, NEW_EMAIL):
            self.assertNotIn(value, logged)

    def test_conflicts_and_forbidden_fields_through_the_bff(self) -> None:
        """CA2, CA3 y CA4 por el BFF, sin modificar la cuenta."""
        provider = self.session_for("service_provider", "profesional.hu002@example.com")

        taken = self.put_account(
            provider,
            {"expected_version": 1, "full_name": "Pro", "email": "deportista.hu005@example.com"},
        )
        stale = self.put_account(
            provider, {"expected_version": 4, "full_name": "Pro", "email": "pro@example.com"}
        )
        forbidden = self.put_account(
            provider,
            {
                "expected_version": 1,
                "full_name": "Pro",
                "email": "pro@example.com",
                "user_id": self.athlete["user_id"],
                "actor_type": "athlete",
                "status": "disabled",
                "password": "otra-contraseña-larga",
            },
        )
        anonymous = self.bff.put("/v1/me/account", json={})

        self.assertEqual(
            (taken.status_code, taken.json()["code"]), (409, "email_already_registered")
        )
        self.assertEqual(taken.json()["details"], [])
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "version_conflict"))
        self.assertEqual(stale.json()["details"], [{"field": "expected_version", "issue": "1"}])
        self.assertEqual(forbidden.status_code, 422, forbidden.text)
        self.assertEqual(
            {(item["field"], item["issue"]) for item in forbidden.json()["details"]},
            {
                ("user_id", "field_not_allowed"),
                ("actor_type", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("password", "field_not_allowed"),
            },
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.updated_events(), 0)
