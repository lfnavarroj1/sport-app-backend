"""El OpenAPI generado debe coincidir con el contrato versionado.

Si cambia de forma intencional: `uv run python scripts/task.py openapi-export`
y revise el diff del contrato junto con su versión.
"""

import unittest
from pathlib import Path

from sportapp_platform.testing.contracts import (
    openapi_contract_path,
    render_openapi,
    repository_root,
)
from training_management.main import create_app
from training_management.settings import TrainingManagementSettings


class OpenApiContractTest(unittest.TestCase):
    def test_generated_openapi_matches_versioned_contract(self) -> None:
        contract = openapi_contract_path(
            repository_root(Path(__file__).resolve()), "training-management"
        )
        app = create_app(
            TrainingManagementSettings(
                app_env="test",
                database_write_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
                database_read_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
                users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            )
        )

        self.assertTrue(contract.is_file(), f"Falta el contrato {contract}")
        self.assertEqual(render_openapi(app), contract.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
