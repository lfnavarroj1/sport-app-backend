"""Solicitud de coordinación 1 de HU010: `bff-web` publica para `POST /v1/training-plans` los
mismos motivos estables de `training_plan_invalid` que `training-management`.

Compara los OpenAPI versionados (el BFF reenvía el `422` del servicio sin reinterpretarlo).
"""

import json
import unittest
from pathlib import Path
from typing import Any

from bff_web.api.training import TRAINING_PLAN_RULE_ISSUES
from sportapp_platform.testing.contracts import openapi_contract_path, repository_root

PATH, METHOD, STATUS = "/v1/training-plans", "post", "422"


def _description(component: str) -> str:
    path = openapi_contract_path(repository_root(Path(__file__).resolve()), component)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    description: str = document["paths"][PATH][METHOD]["responses"][STATUS]["description"]
    return description


class TrainingPlanIssuesContractTest(unittest.TestCase):
    def test_bff_and_service_publish_the_same_rule_issues(self) -> None:
        service = _description("training-management")
        bff = _description("bff-web")

        self.assertEqual(bff, service)
        for issue in TRAINING_PLAN_RULE_ISSUES:
            with self.subTest(issue=issue):
                self.assertIn(f"`{issue}`", service)


if __name__ == "__main__":
    unittest.main()
