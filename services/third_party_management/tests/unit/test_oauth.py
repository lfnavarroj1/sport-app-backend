"""Vinculación OAuth con Strava (ADR-042 §5-§6): CA1 (URL y `state`), CA2 (`LINKED` con
tokens cifrados y `consent_version`) y CA3 (`DENIED`/`INVALID_STATE` sin vinculación)."""

import unittest
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fakes import (
    CAPABILITY,
    CONSENT_VERSION,
    FakeCapabilities,
    FixedClock,
    InMemoryStore,
    InMemoryUnitOfWork,
    strava_context,
)
from leak_checks import leaks

from third_party_management.application.errors import (
    CapabilityRequired,
    PermissionRequired,
    StravaNotConfigured,
)
from third_party_management.application.oauth import (
    CallbackInput,
    CallbackResult,
    CompleteCallback,
    StartAuthorization,
)
from third_party_management.application.ports import Caller
from third_party_management.domain.connections import AccountStatus
from third_party_management.testing.strava_simulator import (
    StravaSimulator,
    server_error,
    status,
    timeout,
)

SCOPE = "read,activity:read"


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {CAPABILITY}})
        self.strava = StravaSimulator(athlete_id=777)
        self.context = strava_context(self.strava)
        self.start = StartAuthorization(
            unit_of_work=lambda: InMemoryUnitOfWork(self.store),
            capabilities=self.capabilities,
            strava=self.context,
            clock=self.clock,
        )
        self.callback = CompleteCallback(
            unit_of_work=lambda: InMemoryUnitOfWork(self.store),
            strava=self.context,
            clock=self.clock,
        )

    def state_for(self, caller: Caller | None = None) -> str:
        url = self.start(caller or self.athlete).authorization_url
        return parse_qs(urlsplit(url).query)["state"][0]

    def complete(self, state: str | None, **overrides: str | None) -> CallbackResult:
        values: dict[str, str | None] = {
            "code": "codigo-sintetico",
            "state": state,
            "scope": SCOPE,
            "error": None,
        }
        values.update(overrides)
        return self.callback(CallbackInput(**values))  # type: ignore[arg-type]


class StartAuthorizationTest(_Fixture):
    def test_returns_consent_url_with_a_single_use_state(self) -> None:
        """CA1."""
        link = self.start(self.athlete)

        query = parse_qs(urlsplit(link.authorization_url).query)
        self.assertEqual(query["scope"], [SCOPE])
        [stored] = self.store.states.values()
        self.assertEqual(query["state"], [stored.state])
        self.assertEqual(
            (stored.user_id, stored.created_at), (self.athlete.user_id, self.clock.now())
        )
        self.assertEqual(link.expires_at, self.clock.now() + timedelta(minutes=10))
        self.assertTrue(self.context.states.verify(stored.state, self.athlete.user_id))

    def test_expired_states_of_the_user_are_purged(self) -> None:
        old = self.state_for()
        self.clock.advance(timedelta(minutes=11))
        fresh = self.state_for()

        self.assertEqual(set(self.store.states), {fresh})
        self.assertNotEqual(old, fresh)

    def test_requires_athlete_capability_and_configuration(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.start(Caller(uuid4(), "organizer"))
        with self.assertRaises(CapabilityRequired):
            self.start(Caller(uuid4(), "athlete"))
        unconfigured = StartAuthorization(
            unit_of_work=lambda: InMemoryUnitOfWork(self.store),
            capabilities=self.capabilities,
            strava=None,
            clock=self.clock,
        )
        with self.assertRaises(StravaNotConfigured):
            unconfigured(self.athlete)
        self.assertEqual(self.store.states, {})


class CallbackTest(_Fixture):
    def test_links_with_encrypted_tokens_and_consent(self) -> None:
        """CA2: `state` válido, intercambio del `code`, tokens cifrados y versión."""
        state = self.state_for()

        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            result = self.complete(state)

        self.assertIs(result, CallbackResult.LINKED)
        account = self.store.accounts[self.athlete.user_id]
        self.assertEqual(account.status, AccountStatus.ACTIVE)
        self.assertEqual((account.external_athlete_id, account.version), ("777", 1))
        self.assertEqual(account.scopes, ("read", "activity:read"))
        self.assertEqual(account.consent_version, CONSENT_VERSION)
        access, refresh = self.strava.issued_tokens[-1]
        assert account.tokens is not None
        self.assertNotIn(access.encode(), account.tokens.access)
        opened = self.context.sealer.open(self.athlete.user_id, account.tokens)
        self.assertEqual((opened.access_token, opened.refresh_token), (access, refresh))
        self.assertEqual(self.store.states, {})
        [record] = audit.records
        self.assertEqual(record.getMessage(), "audit.strava_link")
        fields = record.fields  # type: ignore[attr-defined]
        self.assertEqual(fields, {"user_id": str(self.athlete.user_id), "result": "LINKED"})
        self.assertEqual(leaks(fields, [*self.strava.sensitive_values(), state]), [])

    def test_repeated_callback_with_the_same_state_is_invalid(self) -> None:
        """CA3: un solo uso."""
        state = self.state_for()
        self.assertIs(self.complete(state), CallbackResult.LINKED)

        self.assertIs(self.complete(state), CallbackResult.INVALID_STATE)
        self.assertEqual(len(self.strava.calls("token")), 1)
        self.assertEqual(self.store.accounts[self.athlete.user_id].version, 1)

    def test_invalid_states(self) -> None:
        """CA3: ausente, mal formado, inexistente, vencido o con MAC de otro deportista."""
        other_state = self.context.states.issue(uuid4())
        cases = {
            "ausente": None,
            "vacío": "",
            "mal formado": "abc",
            "inexistente": other_state,
        }
        for name, state in cases.items():
            with self.subTest(name=name):
                self.assertIs(self.complete(state), CallbackResult.INVALID_STATE)

        expired = self.state_for()
        self.clock.advance(timedelta(minutes=10, seconds=1))
        self.assertIs(self.complete(expired), CallbackResult.INVALID_STATE)
        self.assertNotIn(expired, self.store.states)  # también se consume

        self.assertEqual(self.store.accounts, {})
        self.assertEqual(self.strava.calls("token"), [])

    def test_state_whose_mac_does_not_match_the_stored_user(self) -> None:
        state = self.state_for()
        # La fila apunta a otro deportista (alterada): el MAC no corresponde.
        stored = self.store.states[state]
        self.store.states[state] = type(stored)(state, uuid4(), stored.created_at)

        self.assertIs(self.complete(state), CallbackResult.INVALID_STATE)
        self.assertEqual(self.store.accounts, {})

    def test_state_at_exactly_ten_minutes_is_still_valid(self) -> None:
        state = self.state_for()
        self.clock.advance(timedelta(minutes=10))

        self.assertIs(self.complete(state), CallbackResult.LINKED)

    def test_denied_by_user_without_code_or_without_activity_scope(self) -> None:
        """CA3: rechazo, cancelación o scopes insuficientes → `DENIED` sin vinculación."""
        cases: list[dict[str, str | None]] = [
            {"error": "access_denied", "code": None},
            {"code": None},
            {"code": ""},
            {"scope": "read"},
            {"scope": None},
            {"scope": "read,activity:read_all_typo"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIs(self.complete(self.state_for(), **overrides), CallbackResult.DENIED)
        self.assertEqual(self.store.accounts, {})
        self.assertEqual(self.strava.calls("token"), [])
        self.assertEqual(self.store.states, {})

    def test_extra_scopes_are_kept(self) -> None:
        self.complete(self.state_for(), scope="read,activity:read,profile:read_all")

        self.assertEqual(
            self.store.accounts[self.athlete.user_id].scopes,
            ("read", "activity:read", "profile:read_all"),
        )

    def test_exchange_failures_are_error(self) -> None:
        for outcome in (status(400), server_error(), timeout("read"), status(200, json=[])):
            with self.subTest(outcome=outcome):
                self.strava.queue("token", outcome)
                with self.assertLogs("sportapp.integrations", level="WARNING"):
                    result = self.complete(self.state_for())
                self.assertIs(result, CallbackResult.ERROR)
        self.assertEqual(self.store.accounts, {})

    def test_strava_athlete_linked_to_another_user_is_error(self) -> None:
        first = self.complete(self.state_for())
        other = Caller(uuid4(), "athlete")
        self.capabilities.granted[other.user_id] = {CAPABILITY}

        second = self.complete(self.state_for(other))

        self.assertEqual((first, second), (CallbackResult.LINKED, CallbackResult.ERROR))
        self.assertNotIn(other.user_id, self.store.accounts)

    def test_relink_replaces_tokens_with_next_version(self) -> None:
        self.complete(self.state_for())
        first = self.store.accounts[self.athlete.user_id]
        self.clock.advance(timedelta(days=1))

        self.complete(self.state_for())

        second = self.store.accounts[self.athlete.user_id]
        self.assertEqual(second.version, 2)
        self.assertEqual(
            (second.created_at, second.linked_at), (first.created_at, self.clock.now())
        )
        self.assertNotEqual(second.tokens, first.tokens)

    def test_unconfigured_strava_rejects_the_callback(self) -> None:
        unconfigured = CompleteCallback(
            unit_of_work=lambda: InMemoryUnitOfWork(self.store), strava=None, clock=self.clock
        )
        with self.assertRaises(StravaNotConfigured):
            unconfigured(CallbackInput("c", "s", SCOPE, None))


if __name__ == "__main__":
    unittest.main()
