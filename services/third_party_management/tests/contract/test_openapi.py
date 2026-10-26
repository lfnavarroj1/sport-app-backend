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
from third_party_management.main import create_app
from third_party_management.settings import ThirdPartyManagementSettings


class OpenApiContractTest(unittest.TestCase):
    def test_generated_openapi_matches_versioned_contract(self) -> None:
        contract = openapi_contract_path(
            repository_root(Path(__file__).resolve()), "third-party-management"
        )
        # Mismos valores que `openapi-export` (`scripts/components.py`); las STRAVA_* no
        # cambian el OpenAPI.
        app = create_app(
            ThirdPartyManagementSettings(
                app_env="test",
                database_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
                users_management_base_url="http://users-management.invalid",  # type: ignore[arg-type]
                affiliation_management_base_url="http://affiliation-management.invalid",  # type: ignore[arg-type]
            )
        )

        self.assertTrue(contract.is_file(), f"Falta el contrato {contract}")
        self.assertEqual(render_openapi(app), contract.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
