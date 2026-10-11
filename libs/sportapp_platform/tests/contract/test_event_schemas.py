"""Valida los esquemas de eventos versionados en `contracts/events`.

Todavía no existe ningún evento aprobado; cuando se agregue uno, este control
exige que sea JSON Schema válido y que su nombre incluya la versión (`.vN`).
"""

import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from sportapp_platform.testing.contracts import repository_root

_VERSIONED_NAME = re.compile(r"^[a-z_]+(\.[a-z_]+)+\.v\d+\.schema\.json$")


class EventSchemasContractTest(unittest.TestCase):
    def test_event_schemas_are_valid_and_versioned(self) -> None:
        events_dir = repository_root(Path(__file__).resolve()) / "contracts" / "events"
        self.assertTrue(events_dir.is_dir())

        for schema_path in sorted(events_dir.rglob("*.schema.json")):
            with self.subTest(schema=schema_path.name):
                self.assertRegex(schema_path.name, _VERSIONED_NAME)
                Draft202012Validator.check_schema(
                    json.loads(schema_path.read_text(encoding="utf-8"))
                )


if __name__ == "__main__":
    unittest.main()
