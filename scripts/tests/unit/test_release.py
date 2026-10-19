"""`scripts/release.py`: validación del tag, ancestría en `main`, imágenes del render y
manifiesto de release (sin Docker ni GCP)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import release

COMMIT = "a" * 40
NAMES = [
    "users-management",
    "affiliation-management",
    "complementary-services",
    "training-management",
    "events-management",
    "third-party-management",
    "bff-web",
    "bff-mobile",
]
REGISTRY = "us-central1-docker.pkg.dev/sportapp-prod/backend"


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


class ReleaseVersionTest(unittest.TestCase):
    def test_accepts_semver_and_prerelease(self) -> None:
        cases = {"v1.2.3": "1.2.3", "v0.1.0": "0.1.0", "v2.0.0-rc.1": "2.0.0-rc.1"}
        for tag, version in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(release.release_version(tag), version)

    def test_rejects_anything_else(self) -> None:
        for tag in ("1.2.3", "v1.2", "v01.2.3", "v1.2.3+build.5", "latest", "v1.2.3-", "vX.Y.Z"):
            with self.subTest(tag=tag), self.assertRaises(SystemExit):
                release.release_version(tag)


class VerifyTest(unittest.TestCase):
    """Repositorio Git real en un directorio temporal: `main`, una rama y tags."""

    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        git(self.repo, "config", "user.email", "ci@example.com")
        git(self.repo, "config", "user.name", "CI")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "base")
        self.main_commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1.0.0")
        git(self.repo, "switch", "--quiet", "-c", "feature")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "fuera de main")
        self.feature_commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1.1.0")
        git(self.repo, "switch", "--quiet", "main")

    def run_verify(self, tag: str, commit: str, **extra: str) -> None:
        argv = ["verify", "--tag", tag, "--commit", commit]
        argv += ["--repository-url", str(self.repo)]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", value]
        release.main(argv)

    def test_tag_on_main_is_accepted_and_written_to_the_env_file(self) -> None:
        env_file = self.repo.parent / f"{self.repo.name}.env"
        self.addCleanup(lambda: env_file.unlink(missing_ok=True))

        self.run_verify("v1.0.0", self.main_commit, env_file=str(env_file))

        self.assertEqual(
            env_file.read_text(encoding="utf-8").splitlines(),
            ["RELEASE_TAG=v1.0.0", "RELEASE_VERSION=1.0.0", f"RELEASE_COMMIT={self.main_commit}"],
        )

    def test_commit_outside_main_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_verify("v1.1.0", self.feature_commit)
        self.assertIn("no pertenece", str(raised.exception))

    def test_tag_must_point_to_the_commit(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_verify("v1.0.0", self.feature_commit)
        self.assertIn("apunta a", str(raised.exception))

    def test_invalid_tag_or_commit(self) -> None:
        for tag, commit in (("1.0.0", self.main_commit), ("v1.0.0", "abc123")):
            with self.subTest(tag=tag, commit=commit), self.assertRaises(SystemExit):
                self.run_verify(tag, commit)


class K8sCheckTest(unittest.TestCase):
    def render(self, text: str) -> str:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "render.yaml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def base(self, extra: str = "") -> str:
        lines = "".join(f"        image: sportapp/{name}\n" for name in NAMES)
        return self.render(lines + extra)

    def test_placeholders_and_pinned_external_images_pass(self) -> None:
        pinned = "  - image: postgres:17@sha256:" + "b" * 64 + "\n"
        release.main(["k8s-check", self.base(pinned)])

    def test_rejects_latest_unpinned_external_and_missing_components(self) -> None:
        cases = {
            "latest": self.base("        image: sportapp/bff-web:latest\n"),
            "externa": self.base("        image: postgres:17\n"),
            "faltante": self.render("        image: sportapp/bff-web\n"),
            "etiqueta": self.base("        image: sportapp/bff-web:v1.0.0\n"),
        }
        for name, path in cases.items():
            with self.subTest(case=name), self.assertRaises(SystemExit):
                release.main(["k8s-check", path])

    def test_allowed_local_tag(self) -> None:
        lines = "".join(f"        image: sportapp/{name}:local\n" for name in NAMES)
        release.main(["k8s-check", self.render(lines), "--allowed-tag", "local"])


LOG_OK = """test_a (x.T.test_a) ... ok
----------------------------------------------------------------------
Ran 12 tests in 1.0s

OK
Ran 3 tests in 0.2s

OK (skipped=1)
"""


class TestLogTest(unittest.TestCase):
    def test_counts_runs_and_skips(self) -> None:
        self.assertEqual(
            release.parse_test_log(LOG_OK), {"status": "passed", "tests": 15, "skipped": 1}
        )

    def test_failure_and_empty_log(self) -> None:
        failed = release.parse_test_log("Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n")
        self.assertEqual(failed["status"], "failed")
        with self.assertRaises(SystemExit):
            release.parse_test_log("sin pruebas\n")


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan = self.write(
            "plan.json",
            {
                "schemaVersion": 1,
                "tag": "v1.2.0",
                "version": "1.2.0",
                "commit": COMMIT,
                "registry": REGISTRY,
                "images": [
                    {
                        "component": name,
                        "references": [f"{REGISTRY}/{name}:v1.2.0", f"{REGISTRY}/{name}:{COMMIT}"],
                        "imageId": "sha256:" + "c" * 64,
                    }
                    for name in NAMES
                ],
            },
        )
        self.digests = self.write(
            "digests.json",
            {"images": {name: f"{REGISTRY}/{name}@sha256:{'d' * 64}" for name in NAMES}},
        )
        self.coverage = self.dir / "coverage.xml"
        self.coverage.write_text(
            '<?xml version="1.0" ?><coverage line-rate="0.9612" branch-rate="0.88"/>',
            encoding="utf-8",
        )
        self.logs = []
        for suite in ("unit", "integration", "contract", "component"):
            path = self.dir / f"{suite}.log"
            path.write_text(LOG_OK, encoding="utf-8")
            self.logs.append(f"{suite}={path}")

    def write(self, name: str, document: dict[str, Any]) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def manifest(self, mode: str = "publication", **overrides: Any) -> dict[str, Any]:
        output = self.dir / "out.json"
        argv = ["manifest", "--mode", mode, "--plan", str(overrides.get("plan", self.plan))]
        if mode == "publication":
            argv += ["--digests", str(overrides.get("digests", self.digests))]
        argv += ["--coverage", str(self.coverage), "--output", str(output)]
        argv += ["--build-system", "cloud-build", "--build-id", "b-123"]
        for log in overrides.get("logs", self.logs):
            argv += ["--test-log", log]
        release.main(argv)
        document: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
        return document

    def test_publication_manifest(self) -> None:
        document = self.manifest()

        self.assertEqual(
            {k: document[k] for k in ("schemaVersion", "kind", "version", "tag", "commit")},
            {
                "schemaVersion": 1,
                "kind": "backend-release",
                "version": "1.2.0",
                "tag": "v1.2.0",
                "commit": COMMIT,
            },
        )
        self.assertEqual(document["repository"], "https://github.com/lfnavarroj1/sport-app-backend")
        self.assertEqual(document["contractVersion"], document["contracts"]["bff-web"])
        self.assertEqual(sorted(document["contracts"]), sorted(NAMES))
        self.assertEqual(list(document["images"]), NAMES)
        self.assertTrue(
            all("@sha256:" in uri and ":v1" not in uri for uri in document["images"].values())
        )
        self.assertEqual(document["build"], {"system": "cloud-build", "id": "b-123"})
        self.assertTrue(document["builtAt"].endswith("Z"))
        self.assertEqual(document["testResults"]["suites"]["unit"]["tests"], 15)
        self.assertEqual(document["testResults"]["coverage"]["line"], 96.12)
        self.assertEqual(document["testResults"]["coverage"]["threshold"], 80.0)

    def test_validation_manifest_has_no_published_images(self) -> None:
        document = self.manifest("validation")

        self.assertEqual(document["kind"], "backend-validation")
        self.assertEqual(
            document["images"]["bff-web"], {"imageId": "sha256:" + "c" * 64, "published": False}
        )

    def test_rejects_incomplete_or_mutable_artifacts(self) -> None:
        tagged = {name: f"{REGISTRY}/{name}:v1.2.0" for name in NAMES}
        partial = {name: f"{REGISTRY}/{name}@sha256:{'d' * 64}" for name in NAMES[:-1]}
        failing = self.dir / "failing.log"
        failing.write_text("Ran 1 test in 0.1s\n\nFAILED (errors=1)\n", encoding="utf-8")
        cases: dict[str, dict[str, Any]] = {
            "etiqueta mutable": {"digests": self.write("tagged.json", {"images": tagged})},
            "falta un digest": {"digests": self.write("partial.json", {"images": partial})},
            "falta una suite": {"logs": self.logs[:3]},
            "suite fallida": {"logs": [*self.logs[:3], f"component={failing}"]},
        }
        for name, overrides in cases.items():
            with self.subTest(case=name), self.assertRaises(SystemExit):
                self.manifest(**overrides)

    def test_low_coverage_is_rejected(self) -> None:
        self.coverage.write_text('<coverage line-rate="0.5" branch-rate="0.4"/>', encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.manifest()


class PushTest(unittest.TestCase):
    def test_local_plan_is_never_published(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "plan.json"
        path.write_text(json.dumps({"registry": None, "images": []}), encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            release.main(["push", "--plan", str(path), "--digests", str(path.with_suffix(".d"))])
        self.assertIn("nunca publica", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
