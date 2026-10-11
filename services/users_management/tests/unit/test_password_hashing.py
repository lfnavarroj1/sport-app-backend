import unittest

from argon2 import PasswordHasher

from users_management.infrastructure.security.password_hashing import Argon2idPasswordHasher

PASSWORD = "contraseña-muy-secreta"


class Argon2idPasswordHasherTest(unittest.TestCase):
    def test_hash_uses_argon2id_with_versioned_parameters(self) -> None:
        password_hash = Argon2idPasswordHasher().hash(PASSWORD)

        self.assertTrue(password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$"))
        self.assertNotIn(PASSWORD, password_hash)
        self.assertTrue(PasswordHasher().verify(password_hash, PASSWORD))

    def test_same_password_produces_different_salted_hashes(self) -> None:
        hasher = Argon2idPasswordHasher()

        self.assertNotEqual(hasher.hash(PASSWORD), hasher.hash(PASSWORD))


if __name__ == "__main__":
    unittest.main()
