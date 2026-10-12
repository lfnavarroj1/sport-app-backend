import unittest
from pathlib import Path

import users_management
from sportapp_platform.testing.architecture import find_imports_of, find_layer_violations

PACKAGE_DIR = Path(users_management.__file__).parent
OTHER_COMPONENTS = (
    "affiliation_management",
    "complementary_services",
    "training_management",
    "events_management",
    "third_party_management",
    "bff_web",
    "bff_mobile",
)


class ArchitectureTest(unittest.TestCase):
    def test_layers_point_towards_domain(self) -> None:
        self.assertEqual(find_layer_violations(PACKAGE_DIR), [])

    def test_does_not_import_other_components(self) -> None:
        self.assertEqual(find_imports_of(PACKAGE_DIR, OTHER_COMPONENTS), [])


if __name__ == "__main__":
    unittest.main()
