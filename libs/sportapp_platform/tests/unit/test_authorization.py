"""Contrato `actor-permissions` (ADR-025): contenido exacto de la v2 vigente (ADR-041), la v1
conservada sin cambios, filtrado por canal y validación."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sportapp_platform.authorization import (
    ACTOR_PERMISSIONS_ENV,
    load_actor_permissions,
    resolve_actor_permissions_path,
)
from sportapp_platform.config import ConfigurationError

# Tablas de `arquitectura/navegacion/README.md` (ADR-025): (permiso, HU, canal) de la v1.
ORGANIZER_V1 = {
    ("events.create", "HU021", "web"),
    ("events.attendees.view", "HU022", "web"),
    ("events.calendar.view", "HU023", "web"),
    ("events.publish", "HU024", "web"),
    ("events.pricing.manage", "HU025", "web"),
    ("training.routes.recommend", "HU026", "mobile"),
    ("calendar.integrated.view", "HU018", "web"),
}
# ADR-041: la v2 solo agrega el permiso de autoría de rutas del organizador en web.
ORGANIZER = ORGANIZER_V1 | {("training.routes.author", "HU026", "web")}
SERVICE_PROVIDER = {
    ("services.create", "HU015", "web"),
    ("services.publish", "HU019", "web"),
    ("training.plan.author", "HU010", "web"),
    ("training.history.view_assigned", "HU011", "web"),
    ("health.history.view_assigned", "HU012", "web"),
    ("nutrition.history.view_assigned", "HU013", "web"),
    ("nutrition.plan.author", "HU014", "web"),
    ("calendar.integrated.view", "HU018", "web"),
    ("events.recommend", "HU027", "mobile"),
    ("training.plan.recommend", "HU028", "mobile"),
    ("services.support.recommend", "HU029", "mobile"),
    ("nutrition.routine.recommend", "HU030", "mobile"),
}


class ActorPermissionsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = resolve_actor_permissions_path(Path(__file__))
        self.permissions = load_actor_permissions(self.path)
        self.document = json.loads(self.path.read_text(encoding="utf-8"))

    def test_contract_matches_the_navigation_tables_exactly(self) -> None:
        def entries(actor: str) -> set[tuple[str, str, str]]:
            return {
                (entry["permission"], entry["hu"], channel)
                for entry in self.document["actors"][actor]
                for channel in entry["channels"]
            }

        self.assertEqual(self.permissions.version, "actor-permissions.v2")
        self.assertEqual(set(self.document["actors"]), {"athlete", "organizer", "service_provider"})
        self.assertEqual(
            self.document["actors"]["athlete"], [], "el plan, no el rol, da capacidades"
        )
        self.assertEqual(entries("organizer"), ORGANIZER)
        self.assertEqual(entries("service_provider"), SERVICE_PROVIDER)

    def test_permissions_are_filtered_by_channel(self) -> None:
        """CA8 y CA9: cada canal muestra solo las HU aprobadas para él."""
        organizer_web = self.permissions.for_actor("organizer", "web")
        provider_web = self.permissions.for_actor("service_provider", "web")

        self.assertIn("events.create", organizer_web)
        self.assertIn("training.routes.author", organizer_web)
        self.assertNotIn("training.routes.recommend", organizer_web)
        self.assertEqual(
            self.permissions.for_actor("organizer", "mobile"), ("training.routes.recommend",)
        )
        self.assertIn("services.create", provider_web)
        self.assertIn("services.publish", provider_web)
        self.assertNotIn("events.create", provider_web)
        self.assertEqual(self.permissions.for_actor("athlete", "web"), ())
        self.assertEqual(list(organizer_web), sorted(organizer_web))

    def test_v2_only_adds_the_route_author_permission(self) -> None:
        """ADR-041: la v1 se conserva y la v2 no cambia ningún permiso existente."""
        v1_path = self.path.with_name("actor-permissions.v1.json")
        v1 = json.loads(v1_path.read_text(encoding="utf-8"))
        added = [
            entry
            for actor, entries in self.document["actors"].items()
            for entry in entries
            if entry not in v1["actors"][actor]
        ]

        self.assertEqual(self.path.name, "actor-permissions.v2.json")
        self.assertEqual(load_actor_permissions(v1_path).version, "actor-permissions.v1")
        self.assertEqual(set(v1["actors"]), set(self.document["actors"]))
        for actor, entries in v1["actors"].items():
            with self.subTest(actor=actor):
                self.assertTrue(all(entry in self.document["actors"][actor] for entry in entries))
        self.assertEqual(
            added, [{"permission": "training.routes.author", "hu": "HU026", "channels": ["web"]}]
        )

    def test_unknown_actors_are_not_known(self) -> None:
        """CA10: un actor ausente o inventado nunca se trata como otro rol."""
        for actor in ("admin", "", None, "Athlete"):
            with self.subTest(actor=actor):
                self.assertFalse(self.permissions.is_known_actor(actor))
        self.assertEqual(self.permissions.for_actor("admin", "web"), ())

    def test_env_variable_points_to_the_contract_in_the_image(self) -> None:
        with mock.patch.dict(
            os.environ, {ACTOR_PERMISSIONS_ENV: "/opt/x/actor-permissions.v1.json"}
        ):
            self.assertEqual(
                resolve_actor_permissions_path(), Path("/opt/x/actor-permissions.v1.json")
            )

    def test_missing_contract_stops_startup(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(ConfigurationError),
        ):
            resolve_actor_permissions_path(Path(directory))


class InvalidContractTest(unittest.TestCase):
    def load(self, document: object) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actor-permissions.v1.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            load_actor_permissions(path)

    def test_malformed_contracts_are_rejected(self) -> None:
        cases = {
            "sin versión": {"actors": {"organizer": []}},
            "sin actores": {"version": "v1", "actors": {}},
            "actor no lista": {"version": "v1", "actors": {"organizer": {}}},
            "permiso vacío": {"version": "v1", "actors": {"organizer": [{"permission": ""}]}},
            "canal desconocido": {
                "version": "v1",
                "actors": {"organizer": [{"permission": "x", "channels": ["tv"]}]},
            },
            "no es objeto": [1, 2],
        }
        for name, document in cases.items():
            with self.subTest(name=name), self.assertRaises(ConfigurationError) as error:
                self.load(document)
            self.assertIn(ACTOR_PERMISSIONS_ENV, str(error.exception))

    def test_unreadable_file_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_actor_permissions(Path("no-existe.json"))


if __name__ == "__main__":
    unittest.main()
