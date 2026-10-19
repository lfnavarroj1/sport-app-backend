"""Casos de uso de las secciones del perfil (HU005) con persistencia en memoria."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import (
    CONSENT_VERSION,
    FixedClock,
    InMemoryProfileStore,
    demographic_body,
    physiological_body,
    update_demographic_with,
    update_physiological_with,
)

from users_management.api.profile_payload import parse_demographic, parse_physiological
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    request_fingerprint,
)
from users_management.application.errors import IdempotencyKeyReused
from users_management.application.ports import IdempotencyRecord, StoredProfileSection
from users_management.domain.profile.demographic import DemographicInput
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.section import VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection

# Valores sintéticos que no pueden aparecer dentro de un UUID.
SENSITIVE_VALUES = ("68.5", "36.6", "23.2", "Bogotá", "Ingeniera", "1992-04-18")


def demographic_command(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    return ProfileSectionCommand(
        section_input=parse_demographic(demographic_body(**overrides)),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


def physiological_command(key: UUID | None = None, **overrides: Any) -> ProfileSectionCommand[Any]:
    return ProfileSectionCommand(
        section_input=parse_physiological(physiological_body(**overrides)),
        idempotency_key=key or uuid4(),
        correlation_id=uuid4(),
    )


class UpdateDemographicProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
        self.clock = FixedClock()
        self.update = update_demographic_with(self.store, self.clock)
        self.user_id = uuid4()

    def test_creates_version_1_with_event_and_key_in_one_commit(self) -> None:
        """CA1 y CA4: `201`, versión 1, evento con el estado completo y clave."""
        command = demographic_command()

        result = self.update(self.user_id, command)

        self.assertEqual((result.status, result.replayed), (201, False))
        document = result.document
        self.assertEqual(document["user_id"], str(self.user_id))
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["created_at"], "2026-10-07T12:00:00Z")
        self.assertEqual(document["updated_at"], document["created_at"])
        [event] = self.store.outbox
        self.assertEqual(event.event_type, "perfil.demografico_actualizado.v1")
        self.assertEqual(event.aggregate_type, "demographic_profile")
        self.assertEqual((event.aggregate_id, event.aggregate_version), (self.user_id, 1))
        self.assertEqual(event.causation_id, str(command.idempotency_key))
        self.assertEqual(event.correlation_id, command.correlation_id)
        self.assertEqual(event.schema_version, 1)
        self.assertEqual(event.payload, document)
        record = self.store.idempotency[command.idempotency_key]
        self.assertEqual(record.operation, "update_demographic_profile")
        self.assertEqual(record.resource_id, self.user_id)
        self.assertEqual(record.response, {"status": 201, "body": document})
        self.assertEqual(record.expires_at - record.created_at, timedelta(hours=24))

    def test_replaces_with_expected_version_and_keeps_created_at(self) -> None:
        """CA1 y CA6: `200`, nueva versión y `created_at` original."""
        first = self.update(self.user_id, demographic_command())
        self.clock.advance(timedelta(hours=1))

        second = self.update(self.user_id, demographic_command(expected_version=1, city="Cali"))

        self.assertEqual(second.status, 200)
        self.assertEqual(second.document["version"], 2)
        self.assertEqual(second.document["city"], "Cali")
        self.assertEqual(second.document["created_at"], first.document["created_at"])
        self.assertEqual(second.document["updated_at"], "2026-10-07T13:00:00Z")
        self.assertEqual([event.aggregate_version for event in self.store.outbox], [1, 2])

    def test_version_conflicts_do_not_overwrite(self) -> None:
        """CA6: sin `expected_version` o con otra versión → `version_conflict`."""
        self.update(self.user_id, demographic_command())
        for expected in (None, 2):
            with self.subTest(expected_version=expected):
                with self.assertRaises(VersionConflict) as caught:
                    self.update(self.user_id, demographic_command(expected_version=expected))
                self.assertEqual(caught.exception.current_version, 1)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(len(self.store.idempotency), 1)

    def test_expected_version_without_section_is_a_conflict(self) -> None:
        with self.assertRaises(VersionConflict) as caught:
            self.update(self.user_id, demographic_command(expected_version=1))

        self.assertEqual(caught.exception.current_version, 0)
        self.assertEqual(self.store.sections, {})

    def test_invalid_section_persists_nothing(self) -> None:
        """CA2: todo o nada."""
        with self.assertRaises(InvalidProfileSection):
            self.update(self.user_id, demographic_command(gender="X"))

        self.assertEqual((self.store.sections, self.store.outbox), ({}, []))
        self.assertEqual(self.store.idempotency, {})

    def test_retry_with_same_key_returns_original_status_and_body(self) -> None:
        """CA6: reintento con la misma clave y payload → respuesta original."""
        key = uuid4()
        first = self.update(self.user_id, demographic_command(key))
        self.clock.advance(timedelta(minutes=5))

        retry = self.update(self.user_id, demographic_command(key, city="  Bogotá "))

        self.assertTrue(retry.replayed)
        self.assertEqual((retry.status, retry.document), (201, first.document))
        self.assertEqual(len(self.store.outbox), 1)

    def test_retry_of_a_replacement_returns_200(self) -> None:
        self.update(self.user_id, demographic_command())
        key = uuid4()
        first = self.update(self.user_id, demographic_command(key, expected_version=1))

        retry = self.update(self.user_id, demographic_command(key, expected_version=1))

        self.assertEqual((retry.status, retry.replayed), (200, True))
        self.assertEqual(retry.document, first.document)

    def test_same_key_with_other_payload_or_athlete_is_rejected(self) -> None:
        """CA6: otra solicitud u otro deportista con la misma clave → `idempotency_key_reused`."""
        key = uuid4()
        self.update(self.user_id, demographic_command(key))

        with self.assertRaises(IdempotencyKeyReused):
            self.update(self.user_id, demographic_command(key, city="Cali"))
        with self.assertRaises(IdempotencyKeyReused):
            self.update(uuid4(), demographic_command(key))
        with self.assertRaises(IdempotencyKeyReused):
            update_physiological_with(self.store, self.clock)(
                self.user_id, physiological_command(key)
            )
        self.assertEqual(len(self.store.outbox), 1)

    def test_expired_key_is_reused_as_a_new_request(self) -> None:
        key = uuid4()
        self.update(self.user_id, demographic_command(key))
        self.clock.advance(timedelta(hours=25))

        second = self.update(self.user_id, demographic_command(key, expected_version=1))

        self.assertEqual((second.status, second.replayed), (200, False))
        self.assertEqual(self.store.idempotency[key].response["status"], 200)

    def test_concurrent_request_with_same_key_returns_its_result(self) -> None:
        """Si otra solicitud con la misma clave confirmó primero, se devuelve su resultado."""
        key = uuid4()
        winner = InMemoryProfileStore()
        result_of_winner = update_demographic_with(winner, self.clock)(
            self.user_id, demographic_command(key)
        )

        def concurrent_winner() -> None:
            self.store.sections.update(winner.sections)
            self.store.idempotency.update(winner.idempotency)

        update = update_demographic_with(self.store, self.clock, concurrent_winner)
        result = update(self.user_id, demographic_command(key))

        self.assertTrue(result.replayed)
        self.assertEqual(result.document, result_of_winner.document)
        self.assertEqual(self.store.outbox, [])

    def test_concurrent_creation_loses_with_version_conflict(self) -> None:
        """Dos altas simultáneas: la perdedora recibe `409` con la versión vigente."""
        other = StoredProfileSection(
            section=ProfileSection.DEMOGRAPHIC,
            user_id=self.user_id,
            version=1,
            document={"version": 1, "created_at": "2026-10-07T12:00:00Z"},
        )

        def concurrent_creation() -> None:
            self.store.sections[(ProfileSection.DEMOGRAPHIC, self.user_id)] = other

        update = update_demographic_with(self.store, self.clock, concurrent_creation)
        with self.assertRaises(VersionConflict) as caught:
            update(self.user_id, demographic_command())

        self.assertEqual(caught.exception.current_version, 1)
        self.assertEqual(self.store.outbox, [])

    def test_audit_records_outcome_without_values(self) -> None:
        """CA4: auditoría con sección, versión y resultado; nunca valores."""
        key = uuid4()
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.update(self.user_id, demographic_command(key))
            self.update(self.user_id, demographic_command(key))
            self.update(self.user_id, demographic_command(expected_version=1))
            with self.assertRaises(VersionConflict):
                self.update(self.user_id, demographic_command())

        fields = [record.fields for record in logs.records]  # type: ignore[attr-defined]
        self.assertEqual(
            [(item["result"], item["section_version"]) for item in fields],
            [("created", 1), ("replayed", 1), ("replaced", 2), ("rejected", None)],
        )
        self.assertEqual(fields[-1]["reason"], "version_conflict")
        for item in fields:
            self.assertEqual(item["section"], "demographic")
            self.assertEqual(item["user_id"], str(self.user_id))
            self.assertIsNotNone(item["correlation_id"])
        rendered = " ".join(logs.output) + str(fields)
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, rendered)

    def test_fingerprint_depends_on_operation_athlete_and_payload(self) -> None:
        user_id = uuid4()
        base = request_fingerprint("op", user_id, {"a": 1, "b": 2})

        self.assertEqual(base, request_fingerprint("op", user_id, {"b": 2, "a": 1}))
        self.assertNotEqual(base, request_fingerprint("other", user_id, {"a": 1, "b": 2}))
        self.assertNotEqual(base, request_fingerprint("op", uuid4(), {"a": 1, "b": 2}))
        self.assertNotEqual(base, request_fingerprint("op", user_id, {"a": 1, "b": 3}))

    def test_validate_reports_without_persisting(self) -> None:
        with self.assertRaises(InvalidProfileSection):
            self.update.validate(DemographicInput())
        self.assertEqual(self.store.idempotency, {})


class UpdatePhysiologicalProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
        self.clock = FixedClock()
        self.update = update_physiological_with(self.store, self.clock)
        self.user_id = uuid4()

    def test_creates_with_bmi_and_consent_accepted_at_command_instant(self) -> None:
        """CA1 y CA4: `bmi` calculado, consentimiento con instante del comando y evento."""
        result = self.update(self.user_id, physiological_command())

        document = result.document
        self.assertEqual(result.status, 201)
        self.assertEqual(document["bmi"], "23.2")
        self.assertEqual(
            document["consent"], {"version": CONSENT_VERSION, "accepted_at": "2026-10-07T12:00:00Z"}
        )
        [event] = self.store.outbox
        self.assertEqual(event.event_type, "perfil.fisiologico_actualizado.v1")
        self.assertEqual(event.aggregate_type, "physiological_profile")
        self.assertEqual(event.payload, document)

    def test_retry_after_time_passes_is_still_the_same_request(self) -> None:
        """El instante del consentimiento no forma parte de la huella."""
        key = uuid4()
        first = self.update(self.user_id, physiological_command(key))
        self.clock.advance(timedelta(minutes=1))

        retry = self.update(self.user_id, physiological_command(key))

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.document, first.document)

    def test_invalid_consent_persists_nothing(self) -> None:
        """CA3: sin consentimiento vigente no se persiste nada."""
        with self.assertRaises(InvalidProfileSection) as caught:
            self.update(self.user_id, physiological_command(consent={"version": "old"}))

        self.assertEqual(
            [(item.field, item.issue.value) for item in caught.exception.violations],
            [("consent.version", "unsupported_consent_version")],
        )
        self.assertEqual((self.store.sections, self.store.outbox), ({}, []))

    def test_sections_have_independent_versions(self) -> None:
        update_demographic_with(self.store, self.clock)(self.user_id, demographic_command())
        update_demographic_with(self.store, self.clock)(
            self.user_id, demographic_command(expected_version=1)
        )

        result = self.update(self.user_id, physiological_command())

        self.assertEqual(result.document["version"], 1)

    def test_audit_never_contains_health_values(self) -> None:
        """CA4: ni la auditoría ni los logs contienen valores de salud."""
        with self.assertLogs("sportapp", level="DEBUG") as logs:
            self.update(self.user_id, physiological_command())
            with self.assertRaises(InvalidProfileSection):
                self.update(self.user_id, physiological_command(weight_kg="301.5"))

        fields = [getattr(record, "fields", {}) for record in logs.records]
        self.assertEqual([item.get("result") for item in fields], ["created", "rejected"])
        self.assertEqual(fields[1]["reason"], "validation_error")
        rendered = " ".join(logs.output) + str(fields)
        for value in (*SENSITIVE_VALUES, "301.5", CONSENT_VERSION):
            self.assertNotIn(value, rendered)

    def test_replayed_record_from_another_operation_is_rejected(self) -> None:
        key = uuid4()
        self.store.idempotency[key] = IdempotencyRecord(
            key=key,
            operation="register_user",
            request_fingerprint="x",
            resource_id=self.user_id,
            response={"user_id": str(self.user_id)},
            created_at=self.clock.now(),
            expires_at=self.clock.now() + timedelta(hours=1),
        )

        with self.assertRaises(IdempotencyKeyReused):
            self.update(self.user_id, physiological_command(key))


if __name__ == "__main__":
    unittest.main()
