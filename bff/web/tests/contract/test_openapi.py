"""El OpenAPI generado debe coincidir con el contrato versionado.

Si cambia de forma intencional: `uv run python scripts/task.py openapi-export`
y revise el diff del contrato junto con su versión.
"""

import unittest
from pathlib import Path

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.testing.contracts import (
    openapi_contract_path,
    render_openapi,
    repository_root,
)


class OpenApiContractTest(unittest.TestCase):
    def test_generated_openapi_matches_versioned_contract(self) -> None:
        contract = openapi_contract_path(repository_root(Path(__file__).resolve()), "bff-web")
        app = create_app(
            BffWebSettings(
                app_env="test",
                users_management_base_url="http://users.invalid",
                affiliation_management_base_url="http://affiliation.invalid",
                training_management_base_url="http://training.invalid",
            )
        )

        self.assertTrue(contract.is_file(), f"Falta el contrato {contract}")
        self.assertEqual(render_openapi(app), contract.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
