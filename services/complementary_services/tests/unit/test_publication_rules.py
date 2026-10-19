"""HU019: reglas de publicación, transición y payload de `servicio.publicado.v1`, sin
infraestructura."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from fakes import NOW, interval, service_body

from complementary_services.api.payload import parse_offer
from complementary_services.application.ports import ServiceView
from complementary_services.domain.availability.intervals import AvailabilityInterval
from complementary_services.domain.catalog.offer import (
    ServiceOffer,
    ServiceStatus,
    validate_offer,
)
from complementary_services.domain.catalog.publication import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    published_payload,
    utc_instant,
)
from complementary_services.domain.validation import InvalidServiceOffer

CREATED_AT = NOW - timedelta(days=3)


def draft(**body: object) -> ServiceOffer:
    """Borrador creado hace tres días con las reglas de HU015."""
    offer = validate_offer(parse_offer(service_body(**body)), CREATED_AT)
    return ServiceOffer.draft(service_id=uuid4(), provider_id=uuid4(), offer=offer, now=CREATED_AT)


def window(starts_at: datetime, hours: int = 1) -> AvailabilityInterval:
    return AvailabilityInterval(
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=hours),
        time_zone="America/Bogota",
        capacity=1,
    )


def problems(offer: ServiceOffer) -> set[tuple[str | None, str]]:
    with unittest.TestCase().assertRaises(InvalidServiceOffer) as raised:
        offer.publish(NOW)
    return {(item.field, item.issue.value) for item in raised.exception.violations}


class PublicationRulesTest(unittest.TestCase):
    def test_draft_with_future_interval_is_published_once(self) -> None:
        """CA1: `DRAFT` → `PUBLISHED`, versión 2, `published_at` = `updated_at` = ahora."""
        offer = draft()

        published = offer.publish(NOW)

        self.assertEqual((published.status, published.version), (ServiceStatus.PUBLISHED, 2))
        self.assertEqual((published.published_at, published.updated_at), (NOW, NOW))
        self.assertEqual(published.created_at, CREATED_AT)
        self.assertEqual(published.availability, offer.availability)

    def test_expired_intervals_are_ignored_when_one_is_future(self) -> None:
        past = window(NOW - timedelta(days=2))
        in_progress = window(NOW - timedelta(minutes=30))
        offer = replace(draft(), availability=(past, in_progress, window(NOW + timedelta(days=1))))

        self.assertEqual(offer.publish(NOW).status, ServiceStatus.PUBLISHED)

    def test_without_future_interval_availability_is_required(self) -> None:
        """CA2: sin intervalos futuros (ninguno o solo vencidos) se conserva el borrador."""
        for availability in ((), (window(NOW - timedelta(days=1)), window(NOW))):
            with self.subTest(count=len(availability)):
                self.assertEqual(
                    problems(replace(draft(), availability=availability)),
                    {("availability", "required")},
                )

    def test_stored_offer_is_revalidated_reporting_every_rule(self) -> None:
        """CA2: se enumeran todos los requisitos incumplidos."""
        offer = replace(draft(), name="ab", description="corta", availability=())

        self.assertEqual(
            problems(offer),
            {
                ("name", "invalid_length"),
                ("description", "invalid_length"),
                ("availability", "required"),
            },
        )

    def test_future_intervals_keep_the_creation_rules(self) -> None:
        start = NOW + timedelta(days=1)
        overlapping = (window(start, hours=2), window(start + timedelta(hours=1)))

        self.assertEqual(
            problems(replace(draft(), availability=overlapping)),
            {("availability[1]", "overlapping_interval")},
        )

    def test_only_drafts_transition(self) -> None:
        published = draft().publish(NOW)

        with self.assertRaises(ValueError):
            published.publish(NOW + timedelta(minutes=1))


class PublishedEventTest(unittest.TestCase):
    def test_contract_constants(self) -> None:
        self.assertEqual(
            (EVENT_TYPE, SCHEMA_VERSION, AGGREGATE_TYPE), ("servicio.publicado.v1", 1, "service")
        )

    def test_minimal_payload_in_utc(self) -> None:
        """ADR-027: sin nombre, descripción, tarifa ni disponibilidad."""
        published = draft().publish(NOW)

        self.assertEqual(
            published_payload(published),
            {
                "service_id": str(published.service_id),
                "provider_id": str(published.provider_id),
                "status": "PUBLISHED",
                "published_at": "2026-10-12T15:00:00Z",
            },
        )

    def test_draft_has_no_payload(self) -> None:
        with self.assertRaises(ValueError):
            published_payload(draft())

    def test_utc_instant_normalizes_offsets(self) -> None:
        bogota = datetime(2026, 10, 12, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))

        self.assertEqual(utc_instant(bogota), "2026-10-12T15:00:00.123456Z")


class ServiceViewCompatibilityTest(unittest.TestCase):
    def test_published_at_round_trip(self) -> None:
        published = draft().publish(NOW)

        document = ServiceView(published).to_document()

        self.assertEqual(document["published_at"], "2026-10-12T15:00:00+00:00")
        self.assertEqual(ServiceView.from_document(document).offer, published)
        self.assertIsNone(ServiceView(draft()).to_document()["published_at"])

    def test_documents_saved_before_hu019_are_drafts(self) -> None:
        """Las respuestas idempotentes guardadas por HU015 no tienen `published_at`."""
        document = ServiceView(draft(availability=[interval()])).to_document()
        del document["published_at"]

        self.assertIsNone(ServiceView.from_document(document).offer.published_at)

    def test_instants_are_normalized_to_utc(self) -> None:
        bogota = timezone(timedelta(hours=-5))
        offer = replace(draft(), created_at=CREATED_AT.astimezone(bogota))

        self.assertEqual(
            ServiceView(offer).to_document()["created_at"],
            CREATED_AT.astimezone(UTC).isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
