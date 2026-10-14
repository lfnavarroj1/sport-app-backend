"""HU019 con dobles en memoria: caso de uso de publicación, consultas propias y contrato
HTTP (ADR-027, ADR-038)."""

import unittest
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import test_create_service as hu015
from fakes import (
    NOW,
    FakePermissions,
    FixedClock,
    InMemoryCreationUnitOfWork,
    InMemoryPublicationUnitOfWork,
    InMemoryServiceReader,
    InMemoryStore,
    service_body,
)
from fastapi.testclient import TestClient

from complementary_services.api.payload import parse_offer
from complementary_services.api.publication import build_publication_router
from complementary_services.api.services import build_services_router
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.commands.publish_service import (
    PublishService,
    PublishServiceCommand,
)
from complementary_services.application.errors import (
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.ports import Caller
from complementary_services.application.queries.my_services import (
    GetMyService,
    InvalidServicesQuery,
    ListMyServices,
    MyServicesFilters,
)
from complementary_services.domain.catalog.offer import ServiceStatus
from complementary_services.domain.validation import InvalidServiceOffer
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import JwksUnavailable


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.provider = Caller(uuid4(), "service_provider")
        self.create = CreateService(
            unit_of_work=lambda: InMemoryCreationUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )
        self.publish = PublishService(
            unit_of_work=lambda: InMemoryPublicationUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )

    def draft(self, caller: Caller | None = None, key: UUID | None = None, **body: Any) -> UUID:
        result = self.create(
            caller or self.provider,
            CreateServiceCommand(
                offer=parse_offer(service_body(**body)), idempotency_key=key or uuid4()
            ),
        )
        return result.service.offer.service_id

    def command(self, service_id: UUID, key: UUID | None = None) -> PublishServiceCommand:
        return PublishServiceCommand(
            service_id=service_id, idempotency_key=key or uuid4(), correlation_id=uuid4()
        )


class PublishServiceTest(_Fixture):
    def test_publishes_own_draft_with_event_and_key_together(self) -> None:
        """CA1 y CA5: transición, outbox y clave en la misma confirmación."""
        service_id = self.draft()
        self.clock.advance(timedelta(hours=1))
        command = self.command(service_id)

        result = self.publish(self.provider, command)

        publication = result.publication
        self.assertEqual((publication.status, publication.version), (ServiceStatus.PUBLISHED, 2))
        self.assertEqual(publication.published_at, NOW + timedelta(hours=1))
        self.assertEqual(publication.updated_at, publication.published_at)
        self.assertFalse(result.replayed)
        stored = self.store.services[service_id]
        self.assertEqual((stored.status, stored.version), (ServiceStatus.PUBLISHED, 2))
        [message] = self.store.outbox
        self.assertEqual(
            (message.event_type, message.schema_version, message.aggregate_type),
            ("servicio.publicado.v1", 1, "service"),
        )
        self.assertEqual((message.aggregate_id, message.aggregate_version), (service_id, 2))
        self.assertEqual(message.causation_id, str(command.idempotency_key))
        self.assertEqual(message.correlation_id, command.correlation_id)
        self.assertEqual(message.occurred_at, publication.published_at)
        self.assertEqual(
            message.payload,
            {
                "service_id": str(service_id),
                "provider_id": str(self.provider.user_id),
                "status": "PUBLISHED",
                "published_at": "2026-10-12T16:00:00Z",
            },
        )
        record = self.store.idempotency[(self.provider.user_id, command.idempotency_key)]
        self.assertEqual(record.operation, "publish_service")
        self.assertEqual(record.expires_at, publication.published_at + timedelta(hours=24))

    def test_only_service_providers_publish(self) -> None:
        service_id = self.draft()
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.publish(Caller(self.provider.user_id, actor), self.command(service_id))
        self.assertEqual(self.store.outbox, [])

    def test_foreign_or_missing_service_is_not_found(self) -> None:
        """CA3: no se distingue un servicio ajeno de uno inexistente."""
        foreign = self.draft(Caller(uuid4(), "service_provider"))
        for service_id in (foreign, uuid4()):
            with self.subTest(service_id=service_id), self.assertRaises(ServiceNotFound):
                self.publish(self.provider, self.command(service_id))
        self.assertEqual(self.store.services[foreign].status, ServiceStatus.DRAFT)
        self.assertEqual((self.store.outbox, len(self.store.idempotency)), ([], 1))

    def test_same_key_returns_the_original_response(self) -> None:
        """CA4: reintento con la misma clave."""
        service_id = self.draft()
        key = uuid4()
        first = self.publish(self.provider, self.command(service_id, key))
        self.clock.advance(timedelta(minutes=5))

        retry = self.publish(self.provider, self.command(service_id, key))

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.publication, first.publication)
        self.assertEqual(len(self.store.outbox), 1)

    def test_same_key_for_another_service_or_operation_is_reused(self) -> None:
        """La clave es única por profesional, también frente a la creación de HU015."""
        creation_key = uuid4()
        first, second = self.draft(key=creation_key), self.draft()
        key = uuid4()
        self.publish(self.provider, self.command(first, key))

        with self.assertRaises(IdempotencyKeyReused):
            self.publish(self.provider, self.command(second, key))
        with self.assertRaises(IdempotencyKeyReused):
            self.publish(self.provider, self.command(second, creation_key))
        self.assertEqual(self.store.services[second].status, ServiceStatus.DRAFT)

    def test_already_published_with_another_key_keeps_the_state(self) -> None:
        """CA4: `200` con el estado vigente, sin subir la versión ni emitir otro evento."""
        service_id = self.draft()
        first = self.publish(self.provider, self.command(service_id))
        self.clock.advance(timedelta(hours=2))
        key = uuid4()

        again = self.publish(self.provider, self.command(service_id, key))

        self.assertFalse(again.replayed)
        self.assertFalse(again.transitioned)
        self.assertEqual(again.publication, first.publication)
        self.assertEqual(self.store.services[service_id].version, 2)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertIn((self.provider.user_id, key), self.store.idempotency)

    def test_without_future_interval_the_draft_is_kept(self) -> None:
        """CA2."""
        service_id = self.draft(availability=[])

        with self.assertRaises(InvalidServiceOffer) as raised:
            self.publish(self.provider, self.command(service_id))

        self.assertEqual(
            [(v.field, v.issue.value) for v in raised.exception.violations],
            [("availability", "required")],
        )
        self.assertEqual(self.store.services[service_id].status, ServiceStatus.DRAFT)
        self.assertEqual((self.store.outbox, len(self.store.idempotency)), ([], 1))

    def test_expired_intervals_are_ignored(self) -> None:
        service_id = self.draft()
        offer = self.store.services[service_id]
        [future] = offer.availability
        past = replace(future, starts_at=NOW - timedelta(days=1), ends_at=NOW - timedelta(hours=23))
        self.store.services[service_id] = replace(offer, availability=(past, future))

        result = self.publish(self.provider, self.command(service_id))

        self.assertEqual(result.publication.status, ServiceStatus.PUBLISHED)

    def test_concurrent_retry_with_the_same_key_returns_the_winner(self) -> None:
        service_id = self.draft()
        key = uuid4()
        winner = PublishService(
            unit_of_work=lambda: InMemoryPublicationUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )
        units = iter(
            [
                InMemoryPublicationUnitOfWork(self.store),
                InMemoryPublicationUnitOfWork(
                    self.store, lambda: winner(self.provider, self.command(service_id, key))
                ),
                InMemoryPublicationUnitOfWork(self.store),
            ]
        )
        loser = PublishService(
            unit_of_work=units.__next__, permissions=FakePermissions(), clock=self.clock
        )

        result = loser(self.provider, self.command(service_id, key))

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.outbox), 1)

    def test_expired_key_runs_again_without_another_event(self) -> None:
        service_id = self.draft()
        key = uuid4()
        self.publish(self.provider, self.command(service_id, key))
        self.clock.advance(timedelta(hours=25))

        again = self.publish(self.provider, self.command(service_id, key))

        self.assertFalse(again.replayed)
        self.assertEqual(len(self.store.outbox), 1)

    def test_audit_without_offer_content(self) -> None:
        service_id = self.draft()
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.publish(self.provider, self.command(service_id))
            with self.assertRaises(ServiceNotFound):
                self.publish(self.provider, self.command(uuid4()))

        published = logs.records[0].fields  # type: ignore[attr-defined]
        rejected = logs.records[1].fields  # type: ignore[attr-defined]
        self.assertEqual(logs.records[0].getMessage(), "audit.service_publication")
        self.assertEqual(published["result"], "published")
        self.assertEqual(published["service_id"], str(service_id))
        self.assertEqual(published["provider_id"], str(self.provider.user_id))
        self.assertEqual(
            (rejected["result"], rejected["reason"]), ("rejected", "service_not_found")
        )
        self.assertNotIn("Valoración", str(published))


class MyServicesQueryTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        reader = InMemoryServiceReader(self.store)
        self.list = ListMyServices(reader=reader)
        self.get = GetMyService(reader=reader)

    def test_pages_by_updated_at_and_id_descending(self) -> None:
        created = []
        for _ in range(5):
            created.append(self.draft())
            self.clock.advance(timedelta(minutes=1))
        self.draft(Caller(uuid4(), "service_provider"))  # ajeno

        seen: list[UUID] = []
        cursor = None
        while True:
            page = self.list(self.provider, MyServicesFilters(limit=2, cursor=cursor))
            seen.extend(item.offer.service_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(seen, list(reversed(created)))

    def test_status_filter_and_published_first_after_update(self) -> None:
        draft_id, published_id = self.draft(), self.draft()
        self.clock.advance(timedelta(minutes=1))
        self.publish(self.provider, self.command(published_id))

        everything = self.list(self.provider, MyServicesFilters())
        drafts = self.list(self.provider, MyServicesFilters(status="DRAFT"))

        self.assertEqual(
            [item.offer.service_id for item in everything.items], [published_id, draft_id]
        )
        self.assertEqual([item.offer.service_id for item in drafts.items], [draft_id])
        self.assertIsNone(everything.next_cursor)

    def test_invalid_parameters(self) -> None:
        cases = {
            "status": MyServicesFilters(status="ARCHIVED"),
            "limit": MyServicesFilters(limit=0),
            "cursor": MyServicesFilters(cursor="no-es-un-cursor"),
        }
        for field, filters in cases.items():
            with self.subTest(field=field), self.assertRaises(InvalidServicesQuery) as raised:
                self.list(self.provider, filters)
            self.assertEqual(raised.exception.field, field)
        with self.assertRaises(InvalidServicesQuery) as raised:
            self.list(self.provider, MyServicesFilters(limit=101))
        self.assertEqual(raised.exception.issue.value, "out_of_range")

    def test_only_the_owner_provider_reads(self) -> None:
        service_id = self.draft()

        self.assertEqual(self.get(self.provider, service_id).offer.service_id, service_id)
        with self.assertRaises(ServiceNotFound):
            self.get(Caller(uuid4(), "service_provider"), service_id)
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.list(Caller(self.provider.user_id, actor), MyServicesFilters())
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.get(Caller(self.provider.user_id, actor), service_id)


class PublicationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.provider_id = uuid4()
        self.jwks_down = False
        clock = FixedClock()
        reader = InMemoryServiceReader(self.store)

        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return hu015.VERIFIER.verify(raw)

        app = create_service_app(
            service_name="complementary-services",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_services_router(
                    verify_token=verify,
                    create_service=CreateService(
                        unit_of_work=lambda: InMemoryCreationUnitOfWork(self.store),
                        permissions=FakePermissions(),
                        clock=clock,
                    ),
                ),
                build_publication_router(
                    verify_token=verify,
                    publish_service=PublishService(
                        unit_of_work=lambda: InMemoryPublicationUnitOfWork(self.store),
                        permissions=FakePermissions(),
                        clock=clock,
                    ),
                    list_my_services=ListMyServices(reader=reader),
                    get_my_service=GetMyService(reader=reader),
                ),
            ],
        )
        self.client = TestClient(app)
        self.auth = self.bearer(self.provider_id, "service_provider")

    @staticmethod
    def bearer(user_id: UUID, actor_type: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {hu015.token(user_id, actor_type)}"}

    def create(self, auth: dict[str, str] | None = None, **body: Any) -> str:
        response = self.client.post(
            "/v1/services",
            json=service_body(**body),
            headers={**(auth or self.auth), "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(response.status_code, 201, response.text)
        service_id: str = response.json()["service_id"]
        return service_id

    def publish(self, service_id: str, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(f"/v1/services/{service_id}/publish", headers=sent)

    def test_200_contract_and_replay(self) -> None:
        """CA1 y CA4: respuesta de ADR-027 con instantes UTC `Z`."""
        service_id = self.create()
        key = str(uuid4())

        response = self.publish(service_id, key)
        retry = self.publish(service_id, key)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "service_id": service_id,
                "provider_id": str(self.provider_id),
                "status": "PUBLISHED",
                "version": 2,
                "published_at": "2026-10-12T15:00:00Z",
                "updated_at": "2026-10-12T15:00:00Z",
            },
        )
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(retry.json(), response.json())
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.outbox), 1)

    def test_already_published_with_another_key(self) -> None:
        service_id = self.create()
        first = self.publish(service_id, str(uuid4()))

        again = self.publish(service_id, str(uuid4()))

        self.assertEqual((again.status_code, again.json()), (200, first.json()))
        self.assertNotIn("Idempotent-Replayed", again.headers)
        self.assertEqual(len(self.store.outbox), 1)

    def test_idempotency_key_header(self) -> None:
        service_id = self.create()

        missing = self.publish(service_id)
        invalid = self.publish(service_id, "no-es-uuid")

        self.assertEqual((missing.status_code, missing.json()["code"]), (422, "validation_error"))
        self.assertEqual(
            missing.json()["details"], [{"field": "Idempotency-Key", "issue": "required"}]
        )
        self.assertEqual(
            invalid.json()["details"], [{"field": "Idempotency-Key", "issue": "unknown_value"}]
        )

    def test_errors(self) -> None:
        """CA2 y CA3: requisitos faltantes, servicio ajeno y clave reutilizada."""
        empty = self.create(availability=[])
        foreign = self.create(self.bearer(uuid4(), "service_provider"))
        key = str(uuid4())
        self.publish(self.create(), key)

        incomplete = self.publish(empty, str(uuid4()))
        not_found = self.publish(foreign, str(uuid4()))
        reused = self.publish(empty, key)

        self.assertEqual(incomplete.status_code, 422)
        self.assertEqual(
            incomplete.json()["details"], [{"field": "availability", "issue": "required"}]
        )
        self.assertEqual(
            (not_found.status_code, not_found.json()["code"]), (404, "service_not_found")
        )
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )

    def test_authentication_and_permission(self) -> None:
        service_id = self.create()

        anonymous = self.client.post(f"/v1/services/{service_id}/publish")
        athlete = self.publish(service_id, None, **self.bearer(uuid4(), "athlete"))
        self.jwks_down = True
        with self.assertLogs("sportapp.services", level="WARNING"):
            down = self.publish(service_id, str(uuid4()))

        self.assertEqual(
            (anonymous.status_code, anonymous.headers["WWW-Authenticate"]), (401, "Bearer")
        )
        self.assertEqual(anonymous.json()["code"], "invalid_access_token")
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))

    def test_creation_response_includes_null_published_at(self) -> None:
        response = self.client.post(
            "/v1/services",
            json=service_body(),
            headers={**self.auth, "Idempotency-Key": str(uuid4())},
        )

        self.assertIsNone(response.json()["published_at"])

    def test_my_services_list_and_detail(self) -> None:
        draft_id = self.create()
        published_id = self.create()
        self.publish(published_id, str(uuid4()))
        self.create(self.bearer(uuid4(), "service_provider"))

        listed = self.client.get("/v1/me/services", headers=self.auth)
        drafts = self.client.get("/v1/me/services?status=DRAFT&limit=1", headers=self.auth)
        detail = self.client.get(f"/v1/me/services/{published_id}", headers=self.auth)

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        items = listed.json()["items"]
        self.assertEqual({item["service_id"] for item in items}, {draft_id, published_id})
        self.assertIsNone(listed.json()["next_cursor"])
        self.assertEqual([item["service_id"] for item in drafts.json()["items"]], [draft_id])
        body = detail.json()
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        self.assertEqual((body["status"], body["version"]), ("PUBLISHED", 2))
        self.assertEqual(body["published_at"], "2026-10-12T15:00:00Z")
        self.assertEqual(body["availability"][0]["starts_at"], "2026-10-20T14:00:00Z")
        self.assertEqual(
            body["price"], {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"}
        )

    def test_my_services_pagination(self) -> None:
        for _ in range(3):
            self.create()
        first = self.client.get("/v1/me/services?limit=2", headers=self.auth).json()
        second = self.client.get(
            "/v1/me/services",
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=self.auth,
        ).json()

        ids = [item["service_id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(set(ids)), 3)
        self.assertIsNone(second["next_cursor"])

    def test_my_services_errors(self) -> None:
        foreign = self.create(self.bearer(uuid4(), "service_provider"))
        cases = {
            "status=ARCHIVED": ("query.status", "unknown_value"),
            "limit=0": ("query.limit", "out_of_range"),
            "limit=101": ("query.limit", "out_of_range"),
            "limit=diez": ("query.limit", "unknown_value"),
            "cursor=%25%25": ("query.cursor", "unknown_value"),
        }
        for query, (field, issue) in cases.items():
            with self.subTest(query=query):
                response = self.client.get(f"/v1/me/services?{query}", headers=self.auth)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])
        athlete = self.bearer(uuid4(), "athlete")
        listed = self.client.get("/v1/me/services?limit=diez", headers=athlete)
        detail = self.client.get(f"/v1/me/services/{foreign}", headers=athlete)
        not_found = self.client.get(f"/v1/me/services/{foreign}", headers=self.auth)
        anonymous = self.client.get("/v1/me/services")

        self.assertEqual((listed.status_code, listed.json()["code"]), (403, "permission_required"))
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(
            (not_found.status_code, not_found.json()["code"]), (404, "service_not_found")
        )
        self.assertEqual(anonymous.status_code, 401)


if __name__ == "__main__":
    unittest.main()
