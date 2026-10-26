"""Adaptador HTTPX de Strava con el simulador (ADR-042 §9, HA013): URL de autorización,
intercambio, renovación, revocación, paginación, clasificación de `401`/`403`/`404`/`429`/
`5xx`/timeout/cuerpo ilegible, reintentos con backoff, cuota y circuito."""

import logging
import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
from fakes import CLIENT_ID, REDIRECT_URI, ManualMonotonic, NoSleep, gateway
from leak_checks import leaks

from third_party_management.application.errors import ProviderFailure, ProviderFailureKind
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.infrastructure.strava.client import (
    STRAVA_TIMEOUT,
    quota_exhausted,
)
from third_party_management.testing.strava_simulator import (
    StravaSimulator,
    activity,
    body_of,
    connection_error,
    page,
    rate_limited,
    server_error,
    status,
    timeout,
    unreadable,
)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.strava = StravaSimulator()
        self.sleep = NoSleep()
        self.monotonic = ManualMonotonic()
        self.breaker = CircuitBreaker(clock=self.monotonic)
        self.gateway = gateway(self.strava, breaker=self.breaker, sleep=self.sleep)
        self.access, self.refresh = self.strava.issue_tokens()

    def failure(self, call: object) -> ProviderFailure:
        with self.assertRaises(ProviderFailure) as raised:
            call()  # type: ignore[operator]
        return raised.exception

    def list_page(self, page_number: int = 1) -> object:
        return self.gateway.list_activities(self.access, after=None, page=page_number, per_page=50)


class AuthorizationUrlTest(_Fixture):
    def test_official_consent_url_with_minimal_scopes(self) -> None:
        """CA1: `scope=read,activity:read`, `redirect_uri` exacta y el `state`."""
        url = self.gateway.authorization_url("estado.firmado")

        parts = urlsplit(url)
        self.assertEqual(
            f"{parts.scheme}://{parts.netloc}{parts.path}",
            "https://www.strava.com/oauth/mobile/authorize",
        )
        self.assertEqual(
            parse_qs(parts.query),
            {
                "client_id": [CLIENT_ID],
                "redirect_uri": [REDIRECT_URI],
                "response_type": ["code"],
                "approval_prompt": ["auto"],
                "scope": ["read,activity:read"],
                "state": ["estado.firmado"],
            },
        )

    def test_timeouts_of_adr_042(self) -> None:
        self.assertEqual((STRAVA_TIMEOUT.connect, STRAVA_TIMEOUT.read), (2.0, 5.0))


class TokenOperationsTest(_Fixture):
    def test_exchange_code(self) -> None:
        grant = self.gateway.exchange_code("codigo-sintetico")

        self.assertEqual(grant.external_athlete_id, "12345")
        access, refresh = self.strava.issued_tokens[-1]
        self.assertEqual((grant.tokens.access_token, grant.tokens.refresh_token), (access, refresh))
        self.assertGreater(grant.tokens.expires_at, datetime.now(UTC))
        [request] = self.strava.calls("token")
        self.assertEqual(
            {name: value for name, value in body_of(request).items() if name != "client_secret"},
            {
                "client_id": CLIENT_ID,
                "code": "codigo-sintetico",
                "grant_type": "authorization_code",
            },
        )

    def test_exchange_is_not_retried(self) -> None:
        """El `code` es de un solo uso: un timeout no se reintenta."""
        self.strava.queue("token", timeout("read"))

        failure = self.failure(lambda: self.gateway.exchange_code("c"))

        self.assertIs(failure.kind, ProviderFailureKind.UNAVAILABLE)
        self.assertEqual(len(self.strava.calls("token")), 1)
        self.assertEqual(self.sleep.delays, [])

    def test_exchange_rejected_or_invalid(self) -> None:
        cases = [
            (status(400), ProviderFailureKind.REJECTED),
            (status(401), ProviderFailureKind.UNAUTHORIZED),
            (unreadable(), ProviderFailureKind.INVALID_RESPONSE),
            (page([]), ProviderFailureKind.INVALID_RESPONSE),
            (status(200, json={"access_token": "a"}), ProviderFailureKind.INVALID_RESPONSE),
        ]
        for outcome, kind in cases:
            with self.subTest(kind=kind):
                self.strava.queue("token", outcome)
                self.assertIs(self.failure(lambda: self.gateway.exchange_code("c")).kind, kind)

    def test_refresh_retries_server_errors(self) -> None:
        self.strava.queue("refresh", server_error(502))

        tokens = self.gateway.refresh(self.refresh)

        self.assertEqual(tokens.access_token, self.strava.issued_tokens[-1][0])
        self.assertEqual(len(self.strava.calls("refresh")), 2)
        self.assertEqual(body_of(self.strava.calls("refresh")[0])["grant_type"], "refresh_token")

    def test_deauthorize_has_one_retry(self) -> None:
        self.strava.queue("deauthorize", server_error(), server_error())

        failure = self.failure(lambda: self.gateway.deauthorize(self.access))

        self.assertIs(failure.kind, ProviderFailureKind.UNAVAILABLE)
        self.assertEqual(len(self.strava.calls("deauthorize")), 2)


class ActivitiesTest(_Fixture):
    def test_pages_and_after(self) -> None:
        self.strava.activities = [activity(index) for index in range(1, 61)]

        first = self.gateway.list_activities(self.access, after=None, page=1, per_page=50)
        second = self.gateway.list_activities(
            self.access, after=datetime(2026, 10, 1, tzinfo=UTC), page=2, per_page=50
        )

        self.assertEqual((len(first.records), len(second.records)), (50, 10))
        self.assertFalse(first.quota_exhausted)
        params = [dict(request.url.params) for request in self.strava.calls("activities")]
        self.assertEqual(params[0], {"per_page": "50", "page": "1"})
        self.assertEqual(
            params[1],
            {
                "per_page": "50",
                "page": "2",
                "after": str(int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())),
            },
        )
        self.assertEqual(
            self.strava.calls("activities")[0].headers["Authorization"], f"Bearer {self.access}"
        )

    def test_classifies_client_errors_without_retry(self) -> None:
        cases = [
            (status(401), ProviderFailureKind.UNAUTHORIZED),
            (status(403), ProviderFailureKind.FORBIDDEN),
            (status(404), ProviderFailureKind.REJECTED),
            (status(400), ProviderFailureKind.REJECTED),
            (unreadable(), ProviderFailureKind.INVALID_RESPONSE),
            (status(200, json={"no": "lista"}), ProviderFailureKind.INVALID_RESPONSE),
        ]
        for outcome, kind in cases:
            with self.subTest(kind=kind):
                before = len(self.strava.calls("activities"))
                self.strava.queue("activities", outcome)
                self.assertIs(self.failure(self.list_page).kind, kind)
                self.assertEqual(len(self.strava.calls("activities")), before + 1)
        self.assertEqual(self.sleep.delays, [])

    def test_rate_limited_without_retry_and_logged(self) -> None:
        """CA6: `429` respeta `Retry-After` y `X-RateLimit-Usage` sin reintentar."""
        self.strava.queue("activities", rate_limited(retry_after=900, usage="101,300"))

        with self.assertLogs("sportapp.strava", level="WARNING") as logs:
            failure = self.failure(self.list_page)

        self.assertIs(failure.kind, ProviderFailureKind.RATE_LIMITED)
        self.assertEqual(failure.retry_after_seconds, 900)
        self.assertEqual(len(self.strava.calls("activities")), 1)
        [record] = [item for item in logs.records if item.getMessage() == "strava.rate_limited"]
        fields = record.fields  # type: ignore[attr-defined]
        self.assertEqual(
            (fields["retry_after_seconds"], fields["rate_limit_usage"]), (900, "101,300")
        )

    def test_server_errors_and_timeouts_retry_twice_with_backoff(self) -> None:
        """CA6: hasta 2 reintentos con espera exponencial (jitter 0 en la prueba)."""
        self.strava.queue("activities", server_error(500), timeout("connect"), server_error(503))

        failure = self.failure(self.list_page)

        self.assertIs(failure.kind, ProviderFailureKind.UNAVAILABLE)
        self.assertEqual(len(self.strava.calls("activities")), 3)
        self.assertEqual(self.sleep.delays, [0.5, 1.0])

    def test_recovers_after_a_transient_failure(self) -> None:
        self.strava.activities = [activity(1)]
        self.strava.queue("activities", timeout("read"), connection_error())

        result = self.list_page()

        self.assertEqual(len(result.records), 1)  # type: ignore[attr-defined]
        self.assertEqual(len(self.strava.calls("activities")), 3)

    def test_quota_exhausted_after_a_page(self) -> None:
        self.strava.rate_limit = (100, 1000)
        self.strava.usage = [99, 10]

        result = self.gateway.list_activities(self.access, after=None, page=1, per_page=50)

        self.assertTrue(result.quota_exhausted)

    def test_quota_headers(self) -> None:
        cases = [
            ({"X-RateLimit-Limit": "100,1000", "X-RateLimit-Usage": "50,999"}, False),
            ({"X-RateLimit-Limit": "100,1000", "X-RateLimit-Usage": "50,1000"}, True),
            ({"X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": "100,1"}, True),
            ({"X-RateLimit-Limit": "x", "X-RateLimit-Usage": "1,1"}, False),
            ({}, False),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertIs(quota_exhausted(httpx.Headers(headers)), expected)

    def test_logs_never_contain_tokens(self) -> None:
        self.strava.queue("activities", server_error(), status(401))
        with self.assertLogs("sportapp.strava", level="WARNING") as logs:
            self.failure(self.list_page)

        sensitive = [self.access, self.refresh]
        for record in logs.records:
            self.assertEqual(leaks(record.fields, sensitive), [])  # type: ignore[attr-defined]
            self.assertNotIn(self.access, record.getMessage())


class CircuitBreakerTest(_Fixture):
    def test_opens_after_five_consecutive_failures_for_sixty_seconds(self) -> None:
        """HA013: 5 fallos consecutivos abren el circuito 60 s; abierto no llama."""
        self.strava.queue("activities", *[server_error()] * 5)
        self.failure(self.list_page)  # 3 intentos
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.failure(self.list_page)  # 2 intentos más: se abre en el quinto
        calls = len(self.strava.calls("activities"))
        self.assertEqual(calls, 5)
        self.assertTrue(self.breaker.is_open)

        failure = self.failure(self.list_page)
        self.assertIs(failure.kind, ProviderFailureKind.CIRCUIT_OPEN)
        self.assertEqual(len(self.strava.calls("activities")), calls)

        self.monotonic.value += 59.9
        self.assertIs(self.failure(self.list_page).kind, ProviderFailureKind.CIRCUIT_OPEN)

        self.monotonic.value += 0.2
        self.strava.activities = [activity(1)]
        self.list_page()
        self.assertFalse(self.breaker.is_open)

    def test_failure_after_reopening_window_reopens_immediately(self) -> None:
        breaker = CircuitBreaker(clock=self.monotonic)
        for _ in range(5):
            breaker.record_failure()
        self.monotonic.value += 61
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        self.assertFalse(breaker.allow())

    def test_success_resets_the_count(self) -> None:
        breaker = CircuitBreaker(clock=self.monotonic)
        for _ in range(4):
            breaker.record_failure()
        breaker.record_success()
        for _ in range(4):
            breaker.record_failure()
        self.assertTrue(breaker.allow())


if __name__ == "__main__":
    unittest.main()
