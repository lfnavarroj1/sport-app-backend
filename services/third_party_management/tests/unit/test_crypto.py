"""Cifrado AES-256-GCM de tokens (ADR-042 §3) y `state` OAuth HMAC (ADR-042 §5, CA1)."""

import base64
import os
import secrets
import unittest
from uuid import uuid4

from fakes import NOW

from third_party_management.application.ports import ProviderTokens
from third_party_management.infrastructure.oauth_state import HmacStateCodec
from third_party_management.infrastructure.token_cipher import (
    AesGcmTokenSealer,
    InvalidEncryptionKey,
    TokenCipher,
    UndecryptableToken,
    associated_data,
    decode_key,
    derive_state_key,
)


def encoded_key(size: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(size)).rstrip(b"=").decode()


class TokenCipherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = os.urandom(32)
        self.sealer = AesGcmTokenSealer(self.key)
        self.user_id = uuid4()
        self.tokens = ProviderTokens(secrets.token_urlsafe(24), secrets.token_urlsafe(24), NOW)

    def test_round_trip_without_plain_text_at_rest(self) -> None:
        sealed = self.sealer.seal(self.user_id, self.tokens)

        self.assertNotIn(self.tokens.access_token.encode(), sealed.access)
        self.assertNotIn(self.tokens.refresh_token.encode(), sealed.refresh)
        self.assertEqual(self.sealer.open(self.user_id, sealed), self.tokens)
        # Nonce aleatorio de 12 bytes antepuesto: dos cifrados del mismo token difieren.
        again = self.sealer.seal(self.user_id, self.tokens)
        self.assertNotEqual(again.access, sealed.access)
        self.assertNotIn(self.tokens.access_token, repr(sealed))
        self.assertNotIn(self.tokens.access_token, repr(self.tokens))

    def test_associated_data_binds_user_and_token_kind(self) -> None:
        sealed = self.sealer.seal(self.user_id, self.tokens)
        cipher = TokenCipher(self.key)

        with self.assertRaises(UndecryptableToken):
            self.sealer.open(uuid4(), sealed)
        with self.assertRaises(UndecryptableToken):
            cipher.decrypt(sealed.access, associated_data(self.user_id, b"refresh"))
        self.assertEqual(
            cipher.decrypt(sealed.access, associated_data(self.user_id, b"access")),
            self.tokens.access_token,
        )
        self.assertEqual(associated_data(self.user_id, b"access"), self.user_id.bytes + b":access")

    def test_other_key_or_tampered_ciphertext_fails(self) -> None:
        sealed = self.sealer.seal(self.user_id, self.tokens)
        with self.assertRaises(UndecryptableToken):
            AesGcmTokenSealer(os.urandom(32)).open(self.user_id, sealed)
        tampered = TokenCipher(self.key)
        broken = sealed.access[:-1] + bytes([sealed.access[-1] ^ 1])
        with self.assertRaises(UndecryptableToken):
            tampered.decrypt(broken, associated_data(self.user_id, b"access"))

    def test_decode_key(self) -> None:
        self.assertEqual(len(decode_key(encoded_key())), 32)
        for invalid in (encoded_key(16), encoded_key(48), "no es base64 ☃", ""):
            with self.subTest(invalid=invalid[:8]), self.assertRaises(InvalidEncryptionKey):
                decode_key(invalid)

    def test_state_subkey_differs_from_token_key(self) -> None:
        derived = derive_state_key(self.key)

        self.assertEqual(len(derived), 32)
        self.assertNotEqual(derived, self.key)
        self.assertEqual(derived, derive_state_key(self.key))


class OAuthStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = HmacStateCodec(derive_state_key(os.urandom(32)))
        self.user_id = uuid4()

    def test_issue_and_verify_for_the_same_user(self) -> None:
        state = self.codec.issue(self.user_id)

        random_part, mac = state.split(".")
        self.assertEqual((len(random_part), len(mac)), (43, 43))
        self.assertNotIn("=", state)
        self.assertTrue(self.codec.well_formed(state))
        self.assertTrue(self.codec.verify(state, self.user_id))
        self.assertNotEqual(state, self.codec.issue(self.user_id))

    def test_rejects_other_user_other_key_and_tampering(self) -> None:
        state = self.codec.issue(self.user_id)
        random_part, mac = state.split(".")
        flipped = ("A" if mac[0] != "A" else "B") + mac[1:]

        self.assertFalse(self.codec.verify(state, uuid4()))
        self.assertFalse(HmacStateCodec(os.urandom(32)).verify(state, self.user_id))
        self.assertFalse(self.codec.verify(f"{random_part}.{flipped}", self.user_id))

    def test_malformed_states(self) -> None:
        state = self.codec.issue(self.user_id)
        for malformed in (
            "",
            "abc",
            state + ".x",
            state.replace(".", ""),
            "+" * 43 + "." + "/" * 43,
        ):
            with self.subTest(malformed=malformed[:10]):
                self.assertFalse(self.codec.well_formed(malformed))
                self.assertFalse(self.codec.verify(malformed, self.user_id))


if __name__ == "__main__":
    unittest.main()
