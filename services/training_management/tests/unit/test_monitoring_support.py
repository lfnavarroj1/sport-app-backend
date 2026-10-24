"""HU036: cliente del contexto interno de Usuarios (`birth_date`, ADR-041) con un transporte
simulado y tarea de retención de 90 días con un almacén en memoria."""

import unittest
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta

import httpx
from fakes import NOW, FixedClock

from sportapp_platform.correlation import correlation_id_var
from training_management.application.errors import AthleteContextUnavailable
from training_management.infrastructure.integrations.users import UsersAthleteBirthDates
from training_management.workers.measurement_retention import purge_expired, retention_cutoff

Handler = Callable[[httpx.Request], httpx.Response]


class UsersAthleteBirthDatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Un solo cliente: crear cada `httpx.Client` es costoso en Windows (carga `certifi`).
        cls.handlers: list[Handler] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            return cls.handlers[-1](request)

        cls.http = httpx.Client(transport=httpx.MockTransport(dispatch), base_url="http://users")
        cls.client = UsersAthleteBirthDates(cls.http)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.http.close()

    def respond(self, handler: Handler) -> None:
        self.handlers.append(handler)

    def test_reads_birth_date_and_propagates_correlation(self) -> None:
        athlete_id, correlation = uuid.uuid4(), str(uuid.uuid4())
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "sports": [],
                    "primary_goal": None,
                    "city": None,
                    "country_code": None,
                    "birth_date": "1990-05-17",
                },
            )

        self.respond(handler)
        token = correlation_id_var.set(correlation)
        try:
            self.assertEqual(self.client.birth_date(athlete_id), date(1990, 5, 17))
        finally:
            correlation_id_var.reset(token)
        self.assertEqual(requests[0].url.path, f"/v1/users/{athlete_id}/athlete-context")
        self.assertEqual(requests[0].headers["X-Correlation-ID"], correlation)
        self.assertNotIn("Authorization", requests[0].headers)

    def test_without_demographic_profile_is_none(self) -> None:
        self.respond(lambda _: httpx.Response(200, json={"birth_date": None, "sports": []}))
        self.assertIsNone(self.client.birth_date(uuid.uuid4()))

    def test_athlete_not_yet_in_the_projected_directory_is_none(self) -> None:
        self.respond(lambda _: httpx.Response(404, json={"code": "user_not_found"}))
        self.assertIsNone(self.client.birth_date(uuid.uuid4()))

    def test_any_other_answer_is_unavailable_and_logs_no_values(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lento", request=request)

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caído", request=request)

        cases: list[tuple[Handler, str]] = [
            (timeout, "timeout"),
            (broken, "transport_error"),
            (lambda _: httpx.Response(500, json={"code": "internal"}), "status_500"),
            (lambda _: httpx.Response(200, text="no-json"), "invalid_json"),
            (lambda _: httpx.Response(200, json=[]), "invalid_document"),
            (lambda _: httpx.Response(200, json={"sports": []}), "invalid_document"),
            (lambda _: httpx.Response(200, json={"birth_date": 19900517}), "invalid_document"),
            (lambda _: httpx.Response(200, json={"birth_date": "1990-13-40"}), "invalid_document"),
        ]
        for handler, reason in cases:
            self.respond(handler)
            with (
                self.subTest(reason=reason),
                self.assertLogs("sportapp.training", level="WARNING") as captured,
                self.assertRaises(AthleteContextUnavailable),
            ):
                self.client.birth_date(uuid.uuid4())
            fields = captured.records[0].__dict__["fields"]
            self.assertEqual(fields, {"reason": reason})


class _Store:
    def __init__(self, instants: list[datetime]) -> None:
        self.instants = sorted(instants)
        self.calls: list[tuple[datetime, int]] = []

    def purge_measurements(self, older_than: datetime, batch_size: int) -> int:
        self.calls.append((older_than, batch_size))
        expired = [item for item in self.instants if item < older_than][:batch_size]
        self.instants = [item for item in self.instants if item not in expired]
        return len(expired)


class MeasurementRetentionTest(unittest.TestCase):
    def test_purges_everything_older_than_ninety_days_in_batches(self) -> None:
        cutoff = retention_cutoff(NOW)
        self.assertEqual(cutoff, NOW - timedelta(days=90))
        old = [cutoff - timedelta(seconds=second) for second in range(1, 251)]
        recent = [cutoff, cutoff + timedelta(seconds=1), NOW]
        store = _Store(old + recent)
        self.assertEqual(purge_expired(store, FixedClock(), 100), 250)
        self.assertEqual(store.instants, sorted(recent))
        self.assertEqual([size for _, size in store.calls], [100, 100, 100])
        self.assertEqual({instant for instant, _ in store.calls}, {cutoff})

    def test_nothing_to_purge(self) -> None:
        store = _Store([NOW])
        self.assertEqual(purge_expired(store, FixedClock(), 100), 0)
        self.assertEqual(len(store.calls), 1)


if __name__ == "__main__":
    unittest.main()
