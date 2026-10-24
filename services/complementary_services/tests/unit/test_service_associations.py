"""HU017: asociar servicios médicos deportivos (ADR-038) sin infraestructura.

Trazabilidad: CA1 asociación `ACTIVE` única; CA2 `404 service_not_found` y
`403 capability_required`; CA3 `409 association_exists` y respuesta idempotente; CA4 sin
datos de salud; CA5 `INACTIVE` con `ended_at`, `204` repetido e historial con `ALL`.
"""

import logging
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import test_create_service as hu015
from fakes import NOW, FixedClock
from fastapi.testclient import TestClient
from test_service_availability import FakeCapabilities, offer

from complementary_services.api.associations import build_associations_router, parse_service_id
from complementary_services.application.commands.associate_service import (
    MEDICAL_CAPABILITY,
    AssociateService,
    AssociateServiceCommand,
    DissociateService,
    MedicalAccess,
)
from complementary_services.application.errors import (
    ActiveAssociationConflict,
    AssociationExists,
    AssociationNotFound,
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    ServiceNotFound,
)
from complementary_services.application.ports import (
    AssociatedService,
    AssociationView,
    Caller,
    IdempotencyRecord,
    OwnAssociationsQuery,
)
from complementary_services.application.queries.my_associations import (
    ListMyAssociations,
    MyAssociationsFilters,
    decode_cursor,
)
from complementary_services.application.queries.my_services import InvalidServicesQuery
from complementary_services.domain.association.association import (
    AssociationStatus,
    ServiceAssociation,
    is_associable,
)
from complementary_services.domain.catalog.offer import Category, ServiceOffer, ServiceStatus
from complementary_services.domain.validation import Issue, Violation
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

MEDICAL = frozenset({MEDICAL_CAPABILITY})


def medical(name: str = "Valoración médica deportiva", **overrides: Any) -> ServiceOffer:
    return offer(name, category=overrides.pop("category", Category.SPORTS_MEDICINE), **overrides)


@dataclass
class AssociationStore:
    services: dict[UUID, ServiceOffer] = field(default_factory=dict)
    associations: dict[UUID, ServiceAssociation] = field(default_factory=dict)
    idempotency: dict[tuple[UUID, UUID], IdempotencyRecord] = field(default_factory=dict)


class InMemoryAssociationUnitOfWork:
    """Aplica los cambios solo al confirmar y valida la misma unicidad que PostgreSQL."""

    def __init__(
        self, store: AssociationStore, before_commit: Callable[[], None] | None = None
    ) -> None:
        self.store = store
        self.before_commit = before_commit
        self._associations: list[ServiceAssociation] = []
        self._records: list[IdempotencyRecord] = []
        self._deleted: list[tuple[UUID, UUID]] = []
        self.commits = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._associations, self._records, self._deleted = [], [], []

    def find_idempotency(self, provider_id: UUID, key: UUID) -> IdempotencyRecord | None:
        return self.store.idempotency.get((provider_id, key))

    def delete_idempotency(self, provider_id: UUID, key: UUID) -> None:
        self._deleted.append((provider_id, key))

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        self._records.append(record)

    def service(self, service_id: UUID) -> ServiceOffer | None:
        return self.store.services.get(service_id)

    def active_association(self, athlete_id: UUID, service_id: UUID) -> ServiceAssociation | None:
        return next(
            (
                item
                for item in self.store.associations.values()
                if item.athlete_id == athlete_id
                and item.service_id == service_id
                and item.is_active
            ),
            None,
        )

    def add_association(self, association: ServiceAssociation) -> None:
        self._associations.append(association)

    def lock_association(self, association_id: UUID) -> ServiceAssociation | None:
        return self.store.associations.get(association_id)

    def save_end(self, association: ServiceAssociation) -> None:
        self._associations.append(association)

    def commit(self) -> None:
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook()
        for key in self._deleted:
            self.store.idempotency.pop(key, None)
        if any((r.provider_id, r.key) in self.store.idempotency for r in self._records):
            raise IdempotencyKeyConflict()
        for item in self._associations:
            if item.is_active and any(
                other.association_id != item.association_id
                and other.is_active
                and (other.athlete_id, other.service_id) == (item.athlete_id, item.service_id)
                for other in self.store.associations.values()
            ):
                raise ActiveAssociationConflict()
        for item in self._associations:
            self.store.associations[item.association_id] = item
        for record in self._records:
            self.store.idempotency[(record.provider_id, record.key)] = record
        self.commits += 1


class InMemoryAssociationReader:
    def __init__(self, store: AssociationStore) -> None:
        self.store = store

    def list_own(self, query: OwnAssociationsQuery) -> list[AssociationView]:
        items = sorted(
            (
                item
                for item in self.store.associations.values()
                if item.athlete_id == query.athlete_id
                and (query.status is None or item.status is query.status)
                and (
                    query.after is None
                    or (item.associated_at, item.association_id)
                    < (query.after.associated_at, query.after.association_id)
                )
            ),
            key=lambda item: (item.associated_at, item.association_id),
            reverse=True,
        )
        return [
            AssociationView(item, AssociatedService.of(self.store.services[item.service_id]))
            for item in items[: query.limit]
        ]


class DomainTest(unittest.TestCase):
    def test_only_published_sports_medicine_is_associable(self) -> None:
        """CA2: borrador, otra categoría o inexistente no se asocian."""
        self.assertTrue(is_associable(medical()))
        self.assertFalse(is_associable(medical(status=ServiceStatus.DRAFT)))
        self.assertFalse(is_associable(medical(category=Category.COACHING)))
        self.assertFalse(is_associable(None))
        with self.assertRaises(ValueError):
            ServiceAssociation.associate(
                association_id=uuid4(),
                offer=medical(category=Category.NUTRITION),
                athlete_id=uuid4(),
                now=NOW,
            )

    def test_end_is_idempotent_and_keeps_history(self) -> None:
        """CA5: `ACTIVE` → `INACTIVE` con `ended_at` una sola vez."""
        active = ServiceAssociation.associate(
            association_id=uuid4(), offer=medical(), athlete_id=uuid4(), now=NOW
        )
        ended = active.end(NOW + timedelta(hours=1))
        again = ended.end(NOW + timedelta(hours=2))

        self.assertEqual((active.status, active.version, active.ended_at), ("ACTIVE", 1, None))
        self.assertEqual(
            (ended.status, ended.version, ended.ended_at),
            (AssociationStatus.INACTIVE, 2, NOW + timedelta(hours=1)),
        )
        self.assertIs(again, ended)


class AssociationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AssociationStore()
        self.service = medical()
        self.store.services[self.service.service_id] = self.service
        self.capabilities = FakeCapabilities(MEDICAL)
        self.clock = FixedClock()
        self.access = MedicalAccess(self.capabilities)
        self.before_commit: Callable[[], None] | None = None
        self.associate = AssociateService(
            unit_of_work=self.unit_of_work, access=self.access, clock=self.clock
        )
        self.dissociate = DissociateService(
            unit_of_work=self.unit_of_work, access=self.access, clock=self.clock
        )
        self.athlete = Caller(uuid4(), "athlete")

    def unit_of_work(self) -> InMemoryAssociationUnitOfWork:
        return InMemoryAssociationUnitOfWork(self.store, self.run_concurrent)

    def run_concurrent(self) -> None:
        """Simula otra solicitud que confirma justo antes del primer COMMIT."""
        hook, self.before_commit = self.before_commit, None
        if hook is not None:
            hook()

    def command(self, service_id: UUID | None = None, key: UUID | None = None) -> Any:
        return AssociateServiceCommand(
            service_id=service_id or self.service.service_id, idempotency_key=key or uuid4()
        )


class AssociateServiceTest(AssociationCase):
    def test_associates_a_published_medical_service(self) -> None:
        """CA1 y CA4: una asociación `ACTIVE` con el resumen del servicio y nada más."""
        result = self.associate(self.athlete, self.command())

        document = result.association.to_document()
        self.assertFalse(result.replayed)
        self.assertEqual(document["status"], "ACTIVE")
        self.assertEqual(document["service_id"], str(self.service.service_id))
        self.assertEqual(document["version"], 1)
        self.assertIsNone(document["ended_at"])
        self.assertEqual(document["associated_at"], "2026-10-12T15:00:00Z")
        self.assertEqual(
            document["service"],
            {
                "name": "Valoración médica deportiva",
                "category": "SPORTS_MEDICINE",
                "modality": "VIRTUAL",
                "fee": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
            },
        )
        self.assertEqual(
            set(document),
            {
                "association_id",
                "service_id",
                "service",
                "status",
                "associated_at",
                "ended_at",
                "version",
            },
            "CA4: sin datos del deportista, de salud ni del profesional",
        )
        self.assertEqual(len(self.store.associations), 1)

    def test_unknown_draft_or_other_category_is_not_found(self) -> None:
        """CA2: no se revela si existe un borrador."""
        draft = medical(status=ServiceStatus.DRAFT)
        coaching = medical(category=Category.COACHING)
        self.store.services.update({draft.service_id: draft, coaching.service_id: coaching})

        for service_id in (uuid4(), draft.service_id, coaching.service_id):
            with self.subTest(service_id=service_id), self.assertRaises(ServiceNotFound):
                self.associate(self.athlete, self.command(service_id))
        self.assertEqual(self.store.associations, {})

    def test_without_capability_or_other_actor_is_forbidden(self) -> None:
        """CA2: sin la capacidad `capability_required`; otro actor `permission_required`."""
        self.capabilities.granted = frozenset()
        with self.assertRaises(CapabilityRequired):
            self.associate(self.athlete, self.command())
        calls = self.capabilities.calls
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.associate(Caller(uuid4(), actor), self.command())
        self.assertEqual(self.capabilities.calls, calls, "otro actor no consulta Afiliación")
        self.assertEqual(self.store.associations, {})

    def test_affiliation_down_or_slow_is_unavailable(self) -> None:
        for failure in (CapabilitiesUnavailable, CapabilitiesTimeout):
            self.capabilities.failure = failure
            with self.subTest(failure=failure.__name__), self.assertRaises(failure):
                self.associate(self.athlete, self.command())
        self.assertEqual(self.store.associations, {})

    def test_active_association_conflicts_and_is_not_duplicated(self) -> None:
        """CA3: la segunda solicitud con otra clave recibe la existente."""
        first = self.associate(self.athlete, self.command())

        with self.assertRaises(AssociationExists) as raised:
            self.associate(self.athlete, self.command())

        self.assertEqual(
            raised.exception.association_id, first.association.association.association_id
        )
        self.assertEqual(len(self.store.associations), 1)

    def test_same_key_returns_original_response(self) -> None:
        """CA3: la `Idempotency-Key` repetida devuelve la respuesta original."""
        key = uuid4()
        first = self.associate(self.athlete, self.command(key=key))
        self.clock.advance(timedelta(hours=23))

        again = self.associate(self.athlete, self.command(key=key))

        self.assertTrue(again.replayed)
        self.assertEqual(again.association, first.association)
        self.assertEqual(len(self.store.associations), 1)

    def test_same_key_other_service_is_reused(self) -> None:
        other = medical("Fisioterapia deportiva")
        self.store.services[other.service_id] = other
        key = uuid4()
        self.associate(self.athlete, self.command(key=key))

        with self.assertRaises(IdempotencyKeyReused):
            self.associate(self.athlete, self.command(other.service_id, key))

    def test_key_is_per_athlete_and_expires_after_24_hours(self) -> None:
        key = uuid4()
        first = self.associate(self.athlete, self.command(key=key))
        someone_else = self.associate(Caller(uuid4(), "athlete"), self.command(key=key))
        self.dissociate(self.athlete, first.association.association.association_id)
        self.clock.advance(timedelta(hours=24))

        renewed = self.associate(self.athlete, self.command(key=key))

        self.assertFalse(someone_else.replayed)
        self.assertFalse(renewed.replayed)
        self.assertNotEqual(
            renewed.association.association.association_id,
            first.association.association.association_id,
        )

    def test_concurrent_association_with_other_key_is_exists(self) -> None:
        """Consistencia: si otra solicitud confirmó primero, `association_exists`."""
        winner: list[UUID] = []

        def concurrent() -> None:
            association = ServiceAssociation.associate(
                association_id=uuid4(), offer=self.service, athlete_id=self.athlete.user_id, now=NOW
            )
            self.store.associations[association.association_id] = association
            winner.append(association.association_id)

        self.before_commit = concurrent

        with self.assertRaises(AssociationExists) as raised:
            self.associate(self.athlete, self.command())

        self.assertEqual(raised.exception.association_id, winner[0])
        self.assertEqual(len(self.store.associations), 1)

    def test_concurrent_retry_with_same_key_returns_original(self) -> None:
        key = uuid4()
        original: list[Any] = []

        def concurrent() -> None:
            original.append(
                AssociateService(
                    unit_of_work=lambda: InMemoryAssociationUnitOfWork(self.store),
                    access=self.access,
                    clock=self.clock,
                )(self.athlete, self.command(key=key))
            )

        self.before_commit = concurrent

        result = self.associate(self.athlete, self.command(key=key))

        self.assertTrue(result.replayed)
        self.assertEqual(result.association, original[0].association)
        self.assertEqual(len(self.store.associations), 1)

    def test_audits_without_sensitive_data(self) -> None:
        with self.assertLogs("sportapp.audit", logging.INFO) as logs:
            result = self.associate(self.athlete, self.command())
            self.dissociate(self.athlete, result.association.association.association_id)

        fields = [record.__dict__["fields"] for record in logs.records]
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["audit.service_association_changed"] * 2,
        )
        self.assertEqual(
            [(item["action"], item["result"]) for item in fields],
            [("associate", "associated"), ("dissociate", "dissociated")],
        )
        for item in fields:
            self.assertEqual(item["athlete_id"], str(self.athlete.user_id))
            self.assertEqual(item["service_id"], str(self.service.service_id))


class DissociateServiceTest(AssociationCase):
    def test_dissociate_is_idempotent_and_keeps_history(self) -> None:
        """CA5: `INACTIVE` con `ended_at`; repetirlo no cambia nada."""
        created = self.associate(self.athlete, self.command())
        association_id = created.association.association.association_id
        self.clock.advance(timedelta(minutes=5))

        ended = self.dissociate(self.athlete, association_id)
        self.clock.advance(timedelta(minutes=5))
        again = self.dissociate(self.athlete, association_id)

        self.assertEqual(ended.status, AssociationStatus.INACTIVE)
        self.assertEqual(ended.ended_at, NOW + timedelta(minutes=5))
        self.assertEqual(again, ended)
        self.assertEqual(self.store.associations[association_id], ended)

    def test_reassociating_after_dissociation_creates_a_new_active(self) -> None:
        first = self.associate(self.athlete, self.command())
        self.dissociate(self.athlete, first.association.association.association_id)

        second = self.associate(self.athlete, self.command())

        self.assertEqual(len(self.store.associations), 2)
        self.assertEqual(
            sorted(item.status for item in self.store.associations.values()),
            ["ACTIVE", "INACTIVE"],
        )
        self.assertTrue(second.association.association.is_active)

    def test_unknown_or_foreign_association_is_not_found(self) -> None:
        created = self.associate(self.athlete, self.command())
        stranger = Caller(uuid4(), "athlete")

        for caller, association_id in (
            (self.athlete, uuid4()),
            (stranger, created.association.association.association_id),
        ):
            with self.subTest(caller=caller), self.assertRaises(AssociationNotFound):
                self.dissociate(caller, association_id)
        self.assertTrue(
            self.store.associations[created.association.association.association_id].is_active
        )

    def test_requires_actor_and_capability(self) -> None:
        created = self.associate(self.athlete, self.command())
        association_id = created.association.association.association_id
        self.capabilities.granted = frozenset()

        with self.assertRaises(CapabilityRequired):
            self.dissociate(self.athlete, association_id)
        with self.assertRaises(PermissionRequired):
            self.dissociate(Caller(self.athlete.user_id, "organizer"), association_id)
        self.assertTrue(self.store.associations[association_id].is_active)


class MyAssociationsTest(AssociationCase):
    def setUp(self) -> None:
        super().setUp()
        self.list = ListMyAssociations(
            reader=InMemoryAssociationReader(self.store), access=self.access
        )

    def test_status_filters_and_history(self) -> None:
        """CA5: por defecto las activas; con `ALL` también la desasociada."""
        other = medical("Fisioterapia deportiva")
        self.store.services[other.service_id] = other
        ended = self.associate(self.athlete, self.command())
        self.dissociate(self.athlete, ended.association.association.association_id)
        self.clock.advance(timedelta(minutes=1))
        active = self.associate(self.athlete, self.command(other.service_id))
        self.associate(Caller(uuid4(), "athlete"), self.command(other.service_id))

        def ids(status: str | None) -> list[UUID]:
            page = self.list(self.athlete, MyAssociationsFilters(status=status))
            return [item.association.association_id for item in page.items]

        active_id = active.association.association.association_id
        ended_id = ended.association.association.association_id
        self.assertEqual(ids(None), [active_id])
        self.assertEqual(ids("ACTIVE"), [active_id])
        self.assertEqual(ids("INACTIVE"), [ended_id])
        self.assertEqual(ids("ALL"), [active_id, ended_id])

    def test_keyset_pagination(self) -> None:
        created = []
        for index in range(5):
            service = medical(f"Servicio médico {index}")
            self.store.services[service.service_id] = service
            created.append(self.associate(self.athlete, self.command(service.service_id)))
            self.clock.advance(timedelta(minutes=1))

        first = self.list(self.athlete, MyAssociationsFilters(limit=2))
        assert first.next_cursor is not None
        second = self.list(self.athlete, MyAssociationsFilters(limit=2, cursor=first.next_cursor))
        assert second.next_cursor is not None
        last = self.list(self.athlete, MyAssociationsFilters(limit=2, cursor=second.next_cursor))

        seen = [
            item.association.association_id for page in (first, second, last) for item in page.items
        ]
        expected = [item.association.association.association_id for item in reversed(created)]
        self.assertEqual(seen, expected)
        self.assertIsNone(last.next_cursor)

    def test_invalid_parameters(self) -> None:
        cases = (
            (MyAssociationsFilters(status="active"), "status", Issue.UNKNOWN_VALUE),
            (MyAssociationsFilters(limit=0), "limit", Issue.OUT_OF_RANGE),
            (MyAssociationsFilters(limit=101), "limit", Issue.OUT_OF_RANGE),
            (MyAssociationsFilters(cursor="no-es-un-cursor"), "cursor", Issue.UNKNOWN_VALUE),
        )
        for filters, field_name, issue in cases:
            with self.subTest(filters=filters), self.assertRaises(InvalidServicesQuery) as raised:
                self.list(self.athlete, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), (field_name, issue))
        with self.assertRaises(InvalidServicesQuery):
            decode_cursor("eyJhIjoiMjAyNi0xMC0xMlQxNTowMDowMCIsImkiOiJ4In0")

    def test_requires_actor_and_capability(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.list(Caller(uuid4(), "service_provider"), MyAssociationsFilters())
        self.capabilities.granted = frozenset()
        with self.assertRaises(CapabilityRequired):
            self.list(self.athlete, MyAssociationsFilters())


class BodyParsingTest(unittest.TestCase):
    def test_service_id_shape(self) -> None:
        service_id = uuid4()
        cases: tuple[tuple[object, UUID | None, list[Violation]], ...] = (
            ({"service_id": str(service_id)}, service_id, []),
            (None, None, [Violation(None, Issue.REQUIRED)]),
            ([], None, [Violation(None, Issue.UNKNOWN_VALUE)]),
            ({}, None, [Violation("service_id", Issue.REQUIRED)]),
            ({"service_id": 7}, None, [Violation("service_id", Issue.UNKNOWN_VALUE)]),
            ({"service_id": "x"}, None, [Violation("service_id", Issue.UNKNOWN_VALUE)]),
            (
                {"service_id": str(service_id), "health_record": "x"},
                None,
                [Violation("health_record", Issue.FIELD_NOT_ALLOWED)],
            ),
        )
        for body, expected, violations in cases:
            with self.subTest(body=body):
                self.assertEqual(parse_service_id(body), (expected, violations))


class AssociationsApiTest(AssociationCase):
    def setUp(self) -> None:
        super().setUp()
        app = create_service_app(
            service_name="complementary-services",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_associations_router(
                    verify_token=hu015.VERIFIER.verify,
                    associate_service=self.associate,
                    dissociate_service=self.dissociate,
                    list_my_associations=ListMyAssociations(
                        reader=InMemoryAssociationReader(self.store), access=self.access
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.headers = {"Authorization": f"Bearer {hu015.token(self.athlete.user_id, 'athlete')}"}

    def post(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.headers, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post("/v1/me/service-associations", json=body, headers=sent)

    def test_contract(self) -> None:
        """CA1, CA3 y CA5 por HTTP."""
        body = {"service_id": str(self.service.service_id)}
        key = str(uuid4())

        created = self.post(body, key)
        replayed = self.post(body, key)
        duplicate = self.post(body, str(uuid4()))
        association_id = created.json()["association_id"]
        deleted = self.client.delete(
            f"/v1/me/service-associations/{association_id}", headers=self.headers
        )
        repeated = self.client.delete(
            f"/v1/me/service-associations/{association_id}", headers=self.headers
        )
        history = self.client.get("/v1/me/service-associations?status=ALL", headers=self.headers)
        active = self.client.get("/v1/me/service-associations", headers=self.headers)

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(
            created.headers["Location"], f"/v1/me/service-associations/{association_id}"
        )
        self.assertNotIn("Idempotent-Replayed", created.headers)
        self.assertEqual((replayed.status_code, replayed.json()), (201, created.json()))
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["code"], "association_exists")
        self.assertEqual(
            duplicate.json()["details"], [{"field": "association_id", "issue": association_id}]
        )
        self.assertEqual((deleted.status_code, deleted.content), (204, b""))
        self.assertEqual(repeated.status_code, 204)
        [item] = history.json()["items"]
        self.assertEqual(item["status"], "INACTIVE")
        self.assertTrue(item["ended_at"].endswith("Z"))
        self.assertEqual(item["version"], 2)
        self.assertEqual(history.headers["Cache-Control"], "no-store")
        self.assertEqual(active.json(), {"items": [], "next_cursor": None})

    def test_errors(self) -> None:
        """CA2 y validaciones del transporte."""
        draft = medical(status=ServiceStatus.DRAFT)
        self.store.services[draft.service_id] = draft
        provider = {"Authorization": f"Bearer {hu015.token(uuid4(), 'service_provider')}"}

        not_found = self.post({"service_id": str(draft.service_id)}, str(uuid4()))
        invalid = self.post({"service_id": "x", "notes": "y"}, "no-uuid")
        missing_key = self.post({"service_id": str(self.service.service_id)})
        anonymous = self.client.post("/v1/me/service-associations", json={})
        other_actor = self.client.post("/v1/me/service-associations", json={}, headers=provider)
        unknown = self.client.delete(f"/v1/me/service-associations/{uuid4()}", headers=self.headers)
        bad_path = self.client.delete("/v1/me/service-associations/x", headers=self.headers)
        bad_query = self.client.get("/v1/me/service-associations?status=x", headers=self.headers)
        bad_limit = self.client.get("/v1/me/service-associations?limit=x", headers=self.headers)

        self.assertEqual(
            (not_found.status_code, not_found.json()["code"]), (404, "service_not_found")
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["details"],
            [
                {"field": "notes", "issue": "field_not_allowed"},
                {"field": "service_id", "issue": "unknown_value"},
                {"field": "Idempotency-Key", "issue": "unknown_value"},
            ],
        )
        self.assertEqual(
            missing_key.json()["details"], [{"field": "Idempotency-Key", "issue": "required"}]
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(
            (other_actor.status_code, other_actor.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (unknown.status_code, unknown.json()["code"]), (404, "association_not_found")
        )
        self.assertEqual(bad_path.status_code, 422)
        self.assertEqual(
            bad_query.json()["details"], [{"field": "query.status", "issue": "unknown_value"}]
        )
        self.assertEqual(
            bad_limit.json()["details"], [{"field": "query.limit", "issue": "unknown_value"}]
        )
        self.assertEqual(self.store.associations, {})

    def test_capability_is_reported_before_a_malformed_request(self) -> None:
        """CA2: sin la capacidad `403 capability_required`; Afiliación caída `503`."""
        self.capabilities.granted = frozenset()
        forbidden = [
            self.post({}, None),
            self.client.delete(f"/v1/me/service-associations/{uuid4()}", headers=self.headers),
            self.client.get("/v1/me/service-associations?limit=x", headers=self.headers),
        ]
        self.capabilities.failure = CapabilitiesTimeout
        unavailable = [
            self.post({}, None),
            self.client.delete(f"/v1/me/service-associations/{uuid4()}", headers=self.headers),
            self.client.get("/v1/me/service-associations", headers=self.headers),
        ]

        for response in forbidden:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        for response in unavailable:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
            )


class AssociationViewTest(unittest.TestCase):
    def test_document_round_trip(self) -> None:
        service = medical()
        association = replace(
            ServiceAssociation.associate(
                association_id=uuid4(), offer=service, athlete_id=uuid4(), now=NOW
            ).end(NOW + timedelta(days=1)),
        )
        view = AssociationView(association, AssociatedService.of(service))

        self.assertEqual(
            AssociationView.from_document(view.to_document(), association.athlete_id), view
        )


if __name__ == "__main__":
    unittest.main()
