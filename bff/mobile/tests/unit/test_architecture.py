import unittest
from pathlib import Path

import bff_mobile
from sportapp_platform.testing.architecture import find_imports_of

PACKAGE_DIR = Path(bff_mobile.__file__).parent
# El BFF no posee persistencia ni importa código de microservicios: los consume por HTTP.
FORBIDDEN = (
    "sqlalchemy",
    "psycopg",
    "alembic",
    *(
        "users_management",
        "affiliation_management",
        "complementary_services",
        "training_management",
        "events_management",
        "third_party_management",
    ),
)


class ArchitectureTest(unittest.TestCase):
    def test_bff_has_no_persistence_or_service_imports(self) -> None:
        self.assertEqual(find_imports_of(PACKAGE_DIR, FORBIDDEN), [])


if __name__ == "__main__":
    unittest.main()
