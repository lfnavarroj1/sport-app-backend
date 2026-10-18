"""Comandos de release del backend: validar el tag, construir, publicar y describir artefactos.

Uso: `python scripts/release.py <comando> [opciones]`. Los invocan
`.github/workflows/release-validation.yml` (sin acceso a GCP: nunca publica) y
`cloudbuild.release.yaml` (Cloud Build, iniciado desde GCP: construye y publica). Ninguno
despliega: la promoción a producción pertenece a `sport-app-platform` (Cloud Deploy).

Solo usa la biblioteca estándar. `verify`, `push` y `wait-tcp` también corren con el
Python 3.8 de `gcr.io/cloud-builders/docker` (trae `git` y las credenciales de Artifact
Registry de Cloud Build), por eso este módulo evita sintaxis posterior a 3.8 fuera de las
anotaciones y solo importa `components` dentro de los comandos que lo necesitan.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree  # nosec B405: XML propio (reporte de cobertura)

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"
OPENAPI_DIR = ROOT / "contracts" / "openapi"
REPOSITORY = "https://github.com/lfnavarroj1/sport-app-backend"
SCHEMA_VERSION = 1
# `contractVersion` es la versión del OpenAPI de bff-web, que el frontend compara con su
# `expectedBffContract` (decisión del responsable, 2026-10-18). Los demás van en `contracts`.
CONTRACT_COMPONENT = "bff-web"

# SemVer `vX.Y.Z` o `vX.Y.Z-pre`, sin metadatos de compilación: `+` no es válido en una
# etiqueta de imagen OCI.
_NUMBER = r"(0|[1-9][0-9]*)"
_PRERELEASE = r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
TAG_PATTERN = re.compile(rf"^v(?P<version>{_NUMBER}\.{_NUMBER}\.{_NUMBER}{_PRERELEASE})$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_URI = re.compile(r"^[^\s@:]+(/[^\s@:]+)*@sha256:[0-9a-f]{64}$")
IMAGE_LINE = re.compile(r"^\s*-?\s*image:\s*[\"']?(?P<image>[^\s\"']+)")
RAN_LINE = re.compile(r"^Ran (?P<count>\d+) tests? in ")
RESULT_LINE = re.compile(r"^(?P<status>OK|FAILED)(?: \((?P<details>[^)]*)\))?$")


class ReleaseError(SystemExit):
    """Falla con un mensaje claro y código 1 (detiene el workflow o el build)."""

    def __init__(self, message: str) -> None:
        super().__init__(f"release: {message}")


# ---------------------------------------------------------------- utilidades


def _git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False)


def _run(command: list[str]) -> None:
    print("$ " + " ".join(command), flush=True)
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        raise ReleaseError(f"falló: {' '.join(command[:3])}")


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)


def _read_json(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def release_version(tag: str) -> str:
    """Versión SemVer del tag (`v1.2.3-rc.1` → `1.2.3-rc.1`) o `ReleaseError`."""
    match = TAG_PATTERN.match(tag)
    if match is None:
        raise ReleaseError(f"el tag {tag!r} no cumple SemVer vX.Y.Z o vX.Y.Z-pre")
    return match.group("version")


# ---------------------------------------------------------------- verify


def verify(arguments: argparse.Namespace) -> None:
    """Tag SemVer que apunta al commit, y commit ancestro de `main`."""
    tag, commit = arguments.tag, arguments.commit.lower()
    version = release_version(tag)
    if not COMMIT_PATTERN.match(commit):
        raise ReleaseError(f"el commit {commit!r} no es un SHA completo de 40 caracteres")
    if arguments.repository_url:
        # Copia temporal sin contenido de archivos (`--filter=blob:none`): solo `main` y el tag.
        git_dir = Path(tempfile.mkdtemp(prefix="release-verify-"))
        try:
            _check_git(["init", "--bare", "--quiet"], git_dir)
            # Remoto «promisor» explícito: git 2.25 (builder de Cloud Build) lo exige para
            # `--filter`; las versiones nuevas lo aceptan igual.
            _check_git(["remote", "add", "origin", arguments.repository_url], git_dir)
            for key, value in (
                ("remote.origin.promisor", "true"),
                ("remote.origin.partialclonefilter", "blob:none"),
                ("extensions.partialClone", "origin"),
            ):
                _check_git(["config", key, value], git_dir)
            _check_git(
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--filter=blob:none",
                    "origin",
                    "+refs/heads/main:refs/heads/main",
                    f"+refs/tags/{tag}:refs/tags/{tag}",
                ],
                git_dir,
            )
            _check_ancestry(git_dir, tag, commit, "refs/heads/main")
        finally:
            shutil.rmtree(git_dir, ignore_errors=True)
    else:
        _check_ancestry(ROOT, tag, commit, arguments.main_ref)
    values = {"RELEASE_TAG": tag, "RELEASE_VERSION": version, "RELEASE_COMMIT": commit}
    print(f"OK: {tag} → {commit} pertenece a main")
    if arguments.env_file:
        path = Path(arguments.env_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write("".join(f"{k.lower()}={v}\n" for k, v in values.items()))


def _check_git(arguments: list[str], cwd: Path) -> str:
    result = _git(arguments, cwd)
    if result.returncode != 0:
        raise ReleaseError(f"git {arguments[0]} falló: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _check_ancestry(cwd: Path, tag: str, commit: str, main_ref: str) -> None:
    tagged = _check_git(["rev-parse", f"refs/tags/{tag}^{{commit}}"], cwd)
    if tagged != commit:
        raise ReleaseError(f"el tag {tag} apunta a {tagged}, no al commit {commit}")
    ancestry = _git(["merge-base", "--is-ancestor", commit, main_ref], cwd)
    if ancestry.returncode != 0:
        raise ReleaseError(f"el commit {commit} no pertenece a {main_ref}")


# ---------------------------------------------------------------- k8s-check


def k8s_check(arguments: argparse.Namespace) -> None:
    """Imágenes del render: cada componente aparece y ninguna usa una etiqueta mutable.

    En `deploy/k8s/base` las imágenes del backend son marcadores `sportapp/<componente>` sin
    etiqueta (la plataforma los sustituye por digest); cualquier otra imagen debe estar
    fijada por digest. `latest` nunca se acepta.
    """
    from components import COMPONENTS  # noqa: PLC0415 (ver docstring del módulo)

    expected = {component.image for component in COMPONENTS}
    found: set[str] = set()
    problems: list[str] = []
    for name in arguments.rendered:
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            match = IMAGE_LINE.match(line)
            if match is None:
                continue
            image = match.group("image")
            repository = image.split("@", 1)[0].rsplit(":", 1)[0]
            if image.endswith(":latest"):
                problems.append(f"{name}: etiqueta mutable {image}")
            elif repository in expected:
                found.add(repository)
                tag = image[len(repository) + 1 :] if image != repository else ""
                if tag and tag not in arguments.allowed_tag and "@sha256:" not in image:
                    problems.append(f"{name}: etiqueta no permitida {image}")
            elif "@sha256:" not in image:
                problems.append(f"{name}: imagen externa sin digest {image}")
    missing = sorted(expected - found)
    if missing:
        problems.append("componentes sin carga en el render: " + ", ".join(missing))
    if problems:
        raise ReleaseError("manifiestos inválidos:\n  " + "\n  ".join(problems))
    print(f"OK: {len(found)} componentes en {len(arguments.rendered)} render(s)")


# ---------------------------------------------------------------- images


def images(arguments: argparse.Namespace) -> None:
    """Construye cada componente una sola vez, la prueba y la etiqueta con versión y commit.

    Sin `--registry` las referencias son locales (`sportapp/<componente>`): la validación de
    GitHub nunca publica. La prueba importa la fábrica de la aplicación dentro de la imagen
    construida, sin red; lo que se publica después es exactamente esa imagen.
    """
    from components import COMPONENTS, build_command  # noqa: PLC0415

    version = release_version(arguments.tag)
    if not COMMIT_PATTERN.match(arguments.commit):
        raise ReleaseError("--commit debe ser un SHA completo")
    repository = arguments.registry.rstrip("/") if arguments.registry else "sportapp"
    entries = []
    for component in COMPONENTS:
        base = f"{repository}/{component.name}"
        references = [f"{base}:{arguments.tag}", f"{base}:{arguments.commit}"]
        _run(
            build_command(
                component,
                dockerfile=str(DOCKERFILE),
                references=references,
                version=version,
                revision=arguments.commit,
            )
        )
        module = component.factory.split(":", 1)[0]
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python",
                references[1],
                "-c",
                f"import importlib; importlib.import_module({module!r})",
            ]
        )
        image_id = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", references[1]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        entries.append({"component": component.name, "references": references, "imageId": image_id})
    _write_json(
        Path(arguments.plan),
        {
            "schemaVersion": SCHEMA_VERSION,
            "tag": arguments.tag,
            "version": version,
            "commit": arguments.commit,
            "registry": arguments.registry or None,
            "images": entries,
        },
    )


# ---------------------------------------------------------------- push


def push(arguments: argparse.Namespace) -> None:
    """Publica las imágenes ya construidas y probadas, y resuelve su digest.

    Nunca sobrescribe: si una etiqueta ya existe en el registro, falla. Las dos etiquetas de
    un componente deben resolver al mismo digest.
    """
    plan = _read_json(Path(arguments.plan))
    if not plan.get("registry"):
        raise ReleaseError("el plan no tiene registro: la validación local nunca publica")
    for entry in plan["images"]:
        for reference in entry["references"]:
            exists = subprocess.run(
                ["docker", "manifest", "inspect", reference], capture_output=True, check=False
            )
            if exists.returncode == 0:
                raise ReleaseError(f"{reference} ya existe en el registro; no se sobrescribe")
    digests: dict[str, str] = {}
    for entry in plan["images"]:
        for reference in entry["references"]:
            _run(["docker", "push", reference])
        repository = entry["references"][0].rsplit(":", 1)[0]
        listed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", entry["imageId"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        matches = [d for d in json.loads(listed) if d.startswith(repository + "@sha256:")]
        if len(matches) != 1 or not DIGEST_URI.match(matches[0]):
            raise ReleaseError(f"no se pudo resolver un único digest de {repository}")
        digests[entry["component"]] = matches[0]
        print(f"{entry['component']}: {matches[0]}")
    _write_json(Path(arguments.digests), {"images": digests})


# ---------------------------------------------------------------- manifest


def parse_test_log(text: str) -> dict[str, Any]:
    """Cuenta pruebas de una suite (`unittest -v`, una ejecución por paquete)."""
    tests = skipped = runs = 0
    failed = False
    for line in text.splitlines():
        ran = RAN_LINE.match(line.strip())
        if ran:
            tests += int(ran.group("count"))
            runs += 1
            continue
        result = RESULT_LINE.match(line.strip())
        if result:
            failed = failed or result.group("status") == "FAILED"
            for part in (result.group("details") or "").split(","):
                key, _, value = part.strip().partition("=")
                if key == "skipped" and value.isdigit():
                    skipped += int(value)
    if runs == 0:
        raise ReleaseError("el registro de pruebas no contiene ninguna ejecución")
    return {"status": "failed" if failed else "passed", "tests": tests, "skipped": skipped}


def coverage_summary(path: Path, threshold: float) -> dict[str, Any]:
    root = ElementTree.parse(path).getroot()  # noqa: S314  # nosec B314: reporte propio
    line = round(float(root.get("line-rate", "0")) * 100, 2)
    branch = round(float(root.get("branch-rate", "0")) * 100, 2)
    return {"line": line, "branch": branch, "threshold": threshold}


def _coverage_threshold() -> float:
    import tomllib  # noqa: PLC0415 (Python 3.11+; este comando corre en 3.13)

    with open(ROOT / "pyproject.toml", "rb") as handle:
        settings = tomllib.load(handle)
    return float(settings["tool"]["coverage"]["report"]["fail_under"])


def contract_versions() -> dict[str, str]:
    versions = {}
    for path in sorted(OPENAPI_DIR.glob("*.openapi.json")):
        document = _read_json(path)
        versions[path.name.split(".openapi.json")[0]] = str(document["info"]["version"])
    return versions


def build_manifest(arguments: argparse.Namespace) -> dict[str, Any]:
    from components import COMPONENTS  # noqa: PLC0415

    plan = _read_json(Path(arguments.plan))
    names = [component.name for component in COMPONENTS]
    if [entry["component"] for entry in plan["images"]] != names:
        raise ReleaseError("el plan no contiene exactamente los ocho componentes")
    suites = {}
    for item in arguments.test_log:
        suite, _, log_path = item.partition("=")
        suites[suite] = parse_test_log(Path(log_path).read_text(encoding="utf-8"))
    if sorted(suites) != sorted(("unit", "integration", "contract", "component")):
        raise ReleaseError("faltan registros de las suites unit, integration, contract y component")
    failed = [name for name, result in suites.items() if result["status"] != "passed"]
    if failed:
        raise ReleaseError("hay suites con fallos: " + ", ".join(failed))
    coverage = coverage_summary(Path(arguments.coverage), _coverage_threshold())
    if coverage["line"] < coverage["threshold"]:
        raise ReleaseError(f"cobertura {coverage['line']} % por debajo del umbral")
    contracts = contract_versions()
    if CONTRACT_COMPONENT not in contracts:
        raise ReleaseError("falta el contrato OpenAPI de bff-web")
    images_section: dict[str, Any]
    if arguments.mode == "publication":
        published = _read_json(Path(arguments.digests))["images"] if arguments.digests else {}
        if sorted(published) != sorted(names):
            raise ReleaseError("faltan digests publicados de algún componente")
        bad = [uri for uri in published.values() if not DIGEST_URI.match(uri)]
        if bad:
            raise ReleaseError("URI sin digest sha256: " + ", ".join(bad))
        images_section = {name: published[name] for name in names}
    else:
        images_section = {
            entry["component"]: {"imageId": entry["imageId"], "published": False}
            for entry in plan["images"]
        }
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "backend-release" if arguments.mode == "publication" else "backend-validation",
        "repository": REPOSITORY,
        "version": plan["version"],
        "tag": plan["tag"],
        "commit": plan["commit"],
        "contractVersion": contracts[CONTRACT_COMPONENT],
        "contracts": contracts,
        "builtAt": now.isoformat().replace("+00:00", "Z"),
        "build": {"system": arguments.build_system, "id": arguments.build_id},
        "images": images_section,
        "testResults": {"status": "passed", "suites": suites, "coverage": coverage},
    }


def manifest(arguments: argparse.Namespace) -> None:
    """`backend-release.json` (publicación) o `backend-validation.json` (GitHub)."""
    _write_json(Path(arguments.output), build_manifest(arguments))


# ---------------------------------------------------------------- wait-tcp


def wait_tcp(arguments: argparse.Namespace) -> None:
    """Espera a que PostgreSQL y el emulador de Pub/Sub acepten conexiones."""
    deadline = time.monotonic() + arguments.timeout
    for target in arguments.targets:
        host, _, port = target.rpartition(":")
        while True:
            try:
                with socket.create_connection((host, int(port)), timeout=2):
                    print(f"OK {target}")
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise ReleaseError(f"{target} no aceptó conexiones a tiempo") from None
                time.sleep(1)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser(prog="release")
    commands = parser.add_subparsers(dest="command", required=True)

    sub = commands.add_parser("verify", help="Tag SemVer, tag→commit y commit en main")
    sub.add_argument("--tag", required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--main-ref", default="origin/main")
    sub.add_argument("--repository-url", help="verificar contra una copia temporal del remoto")
    sub.add_argument("--env-file", help="escribe RELEASE_TAG, RELEASE_VERSION y RELEASE_COMMIT")
    sub.set_defaults(handler=verify)

    sub = commands.add_parser("k8s-check", help="Imágenes de manifiestos renderizados")
    sub.add_argument("rendered", nargs="+")
    sub.add_argument("--allowed-tag", action="append", default=[])
    sub.set_defaults(handler=k8s_check)

    sub = commands.add_parser("images", help="Construir, probar y etiquetar (sin publicar)")
    sub.add_argument("--tag", required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--registry", default="")
    sub.add_argument("--plan", required=True)
    sub.set_defaults(handler=images)

    sub = commands.add_parser("push", help="Publicar sin sobrescribir y resolver digests")
    sub.add_argument("--plan", required=True)
    sub.add_argument("--digests", required=True)
    sub.set_defaults(handler=push)

    sub = commands.add_parser("manifest", help="Manifiesto de release o de validación")
    sub.add_argument("--mode", choices=("publication", "validation"), required=True)
    sub.add_argument("--plan", required=True)
    sub.add_argument("--digests")
    sub.add_argument("--coverage", required=True)
    sub.add_argument("--test-log", action="append", default=[], help="suite=ruta")
    sub.add_argument("--build-system", required=True)
    sub.add_argument("--build-id", required=True)
    sub.add_argument("--output", required=True)
    sub.set_defaults(handler=manifest)

    sub = commands.add_parser("wait-tcp", help="Esperar servicios de prueba")
    sub.add_argument("targets", nargs="+", help="host:puerto")
    sub.add_argument("--timeout", type=float, default=120.0)
    sub.set_defaults(handler=wait_tcp)

    arguments = parser.parse_args(argv)
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
