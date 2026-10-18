"""Comandos estables del repositorio backend.

Uso: `uv run python scripts/task.py <comando> [opciones]`. Los workflows de CI
invocan estos mismos comandos; no duplican su lógica en YAML.
"""

import argparse
import os
import secrets as random_secrets
import shutil
import string
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from components import (
    COMPONENTS,
    TEST_PACKAGES,
    build_command,
    by_name,
    new_signing_key_pem,
)

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REPORTS = ROOT / "reports"
COVERAGE_FILE = REPORTS / "coverage" / ".coverage"
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"
LOCAL_OVERLAY = ROOT / "deploy" / "k8s" / "overlays" / "local"
LOCAL_SECRETS = LOCAL_OVERLAY / "secrets.local.env"
LOCAL_SECRETS_EXAMPLE = LOCAL_OVERLAY / "secrets.local.env.example"
# Clave de firma de access tokens de users-management (HU003), solo para el clúster local.
LOCAL_JWT_KEY = LOCAL_OVERLAY / "jwt-private-key.local.pem"
LOCAL_JWT_KEY_EXAMPLE = LOCAL_OVERLAY / "jwt-private-key.local.pem.example"
LOCAL_SECRET_FILES = (
    (LOCAL_SECRETS, LOCAL_SECRETS_EXAMPLE),
    (LOCAL_JWT_KEY, LOCAL_JWT_KEY_EXAMPLE),
)

TEST_DB_CONTAINER = "sportapp-test-postgres"
TEST_DB_PORT = 55432
PUBSUB_CONTAINER = "sportapp-pubsub-emulator"
PUBSUB_PORT = 58085
PUBSUB_IMAGE = (
    "gcr.io/google.com/cloudsdktool/google-cloud-cli:588.0.0-emulators@sha256:"
    "be2f0e582a94c9e06e7a384fe2b3b39d590eeb8da65a110eda991598c100fe80"
)
POSTGRES_IMAGE = (
    "postgres:17.11-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"
)
# kustomize sin kubectl ni clúster (workflows de GitHub y Cloud Build).
KUSTOMIZE_IMAGE = (
    "registry.k8s.io/kustomize/kustomize@sha256:"
    "937e7832dc0b09288b1398399dadb97382a27bbc35bee8fdcac47c7de4fff14d"
)
BASE_MANIFESTS = ROOT / "deploy" / "k8s" / "base"
GITLEAKS_IMAGE = (
    "ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:"
    "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
SUITES = ("unit", "integration", "contract", "component")
TYPED_PACKAGES = ("sportapp_platform", *(component.package for component in COMPONENTS))


def run(command: Sequence[str], *, env: dict[str, str] | None = None) -> None:
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def python(*arguments: str) -> list[str]:
    return [sys.executable, *arguments]


def uv(*arguments: str) -> list[str]:
    executable = os.environ.get("UV") or shutil.which("uv")
    if not executable:
        raise SystemExit("uv no está disponible; ejecute los comandos con `uv run`.")
    return [executable, *arguments]


# ---------------------------------------------------------------- calidad


def lock_check(_: argparse.Namespace) -> None:
    run(uv("lock", "--check"))


def format_code(_: argparse.Namespace) -> None:
    run(python("-m", "ruff", "format", "."))
    run(python("-m", "ruff", "check", "--fix", "."))


def format_check(_: argparse.Namespace) -> None:
    run(python("-m", "ruff", "format", "--check", "."))


def lint(_: argparse.Namespace) -> None:
    run(python("-m", "ruff", "check", "."))


def typecheck(_: argparse.Namespace) -> None:
    packages = [argument for package in TYPED_PACKAGES for argument in ("-p", package)]
    run(python("-m", "mypy", *packages))
    run(python("-m", "mypy", "scripts"), env={**os.environ, "MYPYPATH": "scripts"})


# ---------------------------------------------------------------- pruebas


def test(arguments: argparse.Namespace) -> None:
    environment = {**os.environ, "COVERAGE_FILE": str(COVERAGE_FILE)}
    COVERAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    executed = 0
    for package in TEST_PACKAGES:
        suite_dir = ROOT / package / "tests" / arguments.suite
        if not suite_dir.is_dir():
            continue
        executed += 1
        relative = str(suite_dir.relative_to(ROOT))
        run(
            python(
                "-m",
                "coverage",
                "run",
                f"--rcfile={PYPROJECT}",
                "-m",
                "unittest",
                "discover",
                "-s",
                relative,
                "-t",
                relative,
                "-v",
            ),
            env=environment,
        )
    if executed == 0:
        raise SystemExit(f"No existen pruebas para la suite '{arguments.suite}'.")


def coverage_erase(_: argparse.Namespace) -> None:
    environment = {**os.environ, "COVERAGE_FILE": str(COVERAGE_FILE)}
    run(python("-m", "coverage", "erase", f"--rcfile={PYPROJECT}"), env=environment)


def coverage_report(_: argparse.Namespace) -> None:
    """Combina las ejecuciones y aplica el umbral del 80 % definido en pyproject."""
    environment = {**os.environ, "COVERAGE_FILE": str(COVERAGE_FILE)}
    run(python("-m", "coverage", "combine", f"--rcfile={PYPROJECT}"), env=environment)
    run(python("-m", "coverage", "xml", f"--rcfile={PYPROJECT}"), env=environment)
    run(python("-m", "coverage", "report", f"--rcfile={PYPROJECT}"), env=environment)


def openapi_export(_: argparse.Namespace) -> None:
    """Regenera `contracts/openapi/*.openapi.json` desde el código."""
    for component in COMPONENTS:
        environment = {**os.environ, "APP_ENV": "test", **component.openapi_environment()}
        script = (
            "from pathlib import Path\n"
            f"from {component.package}.main import create_app\n"
            "from sportapp_platform.testing.contracts import (\n"
            "    openapi_contract_path, render_openapi)\n"
            f"path = openapi_contract_path(Path.cwd(), {component.name!r})\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "path.write_text(render_openapi(create_app()), encoding='utf-8', newline='\\n')\n"
            "print(path)\n"
        )
        run(python("-c", script), env=environment)


# ---------------------------------------------------------------- seguridad


def security(_: argparse.Namespace) -> None:
    report_dir = REPORTS / "security"
    report_dir.mkdir(parents=True, exist_ok=True)
    run(python("-m", "bandit", "-c", str(PYPROJECT), "-r", "libs", "services", "bff", "-q"))
    requirements = report_dir / "requirements-runtime.txt"
    run(
        uv(
            "export",
            "--frozen",
            "--no-dev",
            "--all-packages",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
            "--quiet",
        )
    )
    run(python("-m", "pip_audit", "--strict", "--disable-pip", "-r", str(requirements)))


def secrets(arguments: argparse.Namespace) -> None:
    """Escaneo de secretos con Gitleaks (contenedor fijado por digest)."""
    target = ["git", "/repo"] if arguments.mode == "git" else ["dir", "/repo"]
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ROOT}:/repo",
            GITLEAKS_IMAGE,
            *target,
            "--config",
            "/repo/.gitleaks.toml",
            "--redact",
            "--no-banner",
            "--verbose",
        ]
    )


# ---------------------------------------------------------------- artefactos


def build(arguments: argparse.Namespace) -> None:
    selected = COMPONENTS if arguments.component == "all" else (by_name(arguments.component),)
    revision = arguments.revision or "local"
    for component in selected:
        run(
            build_command(
                component,
                dockerfile=str(DOCKERFILE),
                references=(f"{component.image}:{arguments.tag}",),
                version=arguments.version,
                revision=revision,
            )
        )


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def serve_local(arguments: argparse.Namespace) -> None:
    """Ejecuta un componente en el host con recarga; variables desde `.env`."""
    component = by_name(arguments.component)
    environment = {**_load_dotenv(ROOT / ".env"), **os.environ}
    run(
        python(
            "-m",
            "uvicorn",
            "--factory",
            component.factory,
            "--reload",
            "--reload-dir",
            component.directory,
            "--reload-dir",
            "libs/sportapp_platform",
            "--host",
            "127.0.0.1",
            "--port",
            str(arguments.port),
        ),
        env=environment,
    )


def smoke(arguments: argparse.Namespace) -> None:
    """Verifica `/health/live` y `/health/ready` de una instancia desplegada."""
    base_url = arguments.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit("--base-url debe usar http:// o https://")
    for path in ("/health/live", "/health/ready"):
        for attempt in range(1, arguments.retries + 1):
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:  # noqa: S310  # nosec B310
                    if response.status == 200:
                        print(f"OK {path}")
                        break
            except OSError as error:
                print(f"intento {attempt}: {path} no disponible ({type(error).__name__})")
            time.sleep(arguments.interval)
        else:
            raise SystemExit(f"Smoke fallido: {base_url}{path}")


# ---------------------------------------------------------------- entorno local


def db_up(_: argparse.Namespace) -> None:
    """PostgreSQL efímero para pruebas de integración (solo escucha en 127.0.0.1)."""
    run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            TEST_DB_CONTAINER,
            "--publish",
            f"127.0.0.1:{TEST_DB_PORT}:5432",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE,
        ]
    )
    for _attempt in range(30):
        ready = subprocess.run(
            ["docker", "exec", TEST_DB_CONTAINER, "pg_isready", "-U", "postgres"],
            capture_output=True,
            check=False,
        )
        if ready.returncode == 0:
            print(
                f"TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:{TEST_DB_PORT}/postgres"
            )
            return
        time.sleep(1)
    raise SystemExit("PostgreSQL de pruebas no quedó disponible.")


def pubsub_up(_: argparse.Namespace) -> None:
    """Emulador oficial de Pub/Sub para integración (solo escucha en 127.0.0.1)."""
    run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            PUBSUB_CONTAINER,
            "--publish",
            f"127.0.0.1:{PUBSUB_PORT}:8085",
            PUBSUB_IMAGE,
            "gcloud",
            "beta",
            "emulators",
            "pubsub",
            "start",
            "--host-port=0.0.0.0:8085",
        ]
    )
    for _attempt in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PUBSUB_PORT}", timeout=2):  # nosec B310
                print(f"PUBSUB_EMULATOR_HOST=127.0.0.1:{PUBSUB_PORT}")
                return
        except OSError:
            time.sleep(1)
    raise SystemExit("El emulador de Pub/Sub no quedó disponible.")


def pubsub_down(_: argparse.Namespace) -> None:
    run(["docker", "rm", "--force", PUBSUB_CONTAINER])


def db_down(_: argparse.Namespace) -> None:
    run(["docker", "rm", "--force", TEST_DB_CONTAINER])


def k8s_secrets(_: argparse.Namespace) -> None:
    """Genera los secretos del clúster local que falten: contraseñas aleatorias en
    `secrets.local.env` y una clave Ed25519 en `jwt-private-key.local.pem`.

    No imprime los valores y nunca sobrescribe un archivo existente.
    """
    created = 0
    if not LOCAL_SECRETS.is_file():
        alphabet = string.ascii_letters + string.digits
        lines = []
        for line in LOCAL_SECRETS_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                value = "".join(random_secrets.choice(alphabet) for _ in range(32))
                lines.append(f"{key}={value}")
        LOCAL_SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"Creado {LOCAL_SECRETS.relative_to(ROOT)} con {len(lines)} contraseñas locales.")
        created += 1
    if not LOCAL_JWT_KEY.is_file():
        LOCAL_JWT_KEY.write_text(new_signing_key_pem(), encoding="ascii", newline="\n")
        print(f"Creado {LOCAL_JWT_KEY.relative_to(ROOT)} (clave de firma Ed25519 local).")
        created += 1
    if not created:
        raise SystemExit(
            "Los secretos locales ya existen; no se sobrescriben. Borre "
            f"{LOCAL_SECRETS.relative_to(ROOT)} a mano solo si también recreará el volumen "
            f"de PostgreSQL; borrar {LOCAL_JWT_KEY.relative_to(ROOT)} invalida las sesiones."
        )


def _kustomize(source: Path, output: Path, engine: str) -> None:
    if engine == "kubectl":
        run(["kubectl", "kustomize", str(source), "--output", str(output)])
        return
    # `--engine docker`: el kustomize fijado por digest; nunca contacta un clúster.
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT}:/workspace",
            "-w",
            "/workspace",
            KUSTOMIZE_IMAGE,
            "build",
            source.relative_to(ROOT).as_posix(),
            "-o",
            output.relative_to(ROOT).as_posix(),
        ]
    )


def k8s_render(arguments: argparse.Namespace) -> None:
    """Renderiza `deploy/k8s/base` y el overlay local sin aplicarlos a ningún clúster."""
    created_from_example: list[Path] = []
    try:
        for secret_file, example in LOCAL_SECRET_FILES:
            if secret_file.is_file():
                continue
            if not arguments.use_example:
                raise SystemExit(
                    f"Falta {secret_file.relative_to(ROOT)}; genérelo con `k8s-secrets` "
                    "o use --use-example solo para validar el renderizado."
                )
            shutil.copyfile(example, secret_file)
            created_from_example.append(secret_file)
        output = REPORTS / "k8s" / "local.yaml"
        output.parent.mkdir(parents=True, exist_ok=True)
        _kustomize(BASE_MANIFESTS, REPORTS / "k8s" / "base.yaml", arguments.engine)
        _kustomize(LOCAL_OVERLAY, output, arguments.engine)
        print(REPORTS / "k8s" / "base.yaml")
        print(output)
    finally:
        for secret_file in created_from_example:
            secret_file.unlink()


# ---------------------------------------------------------------- agregados de CI


def _sequence(*steps: Callable[[argparse.Namespace], None], **values: str) -> None:
    for step in steps:
        step(argparse.Namespace(**values))


def ci_fast(_: argparse.Namespace) -> None:
    """Equivalente local del pipeline de push y del hook previo al commit."""
    _sequence(lock_check, format_check, lint, typecheck)
    _sequence(test, suite="unit")


def ci_full(_: argparse.Namespace) -> None:
    """Equivalente local de la puerta de pull request (requiere TEST_DATABASE_URL)."""
    _sequence(lock_check, format_check, lint, typecheck, coverage_erase)
    for suite in SUITES:
        _sequence(test, suite=suite)
    _sequence(coverage_report, security)


def main() -> None:
    parser = argparse.ArgumentParser(prog="task")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str, handler: Callable[[argparse.Namespace], None], help_text: str
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    command("lock-check", lock_check, "Verifica que uv.lock corresponda a los pyproject")
    command("format", format_code, "Aplica formato y correcciones automáticas")
    command("format-check", format_check, "Verifica formato sin modificar")
    command("lint", lint, "Ruff")
    command("typecheck", typecheck, "mypy estricto")
    command("test", test, "Ejecuta una suite con cobertura").add_argument("suite", choices=SUITES)
    command("coverage-erase", coverage_erase, "Borra datos de cobertura previos")
    command("coverage", coverage_report, "Combina cobertura y aplica el umbral del 80 %%")
    command("openapi-export", openapi_export, "Regenera los contratos OpenAPI")
    command("security", security, "Bandit (SAST) y pip-audit (dependencias)")
    command("secrets", secrets, "Gitleaks").add_argument(
        "--mode", choices=("dir", "git"), default="dir"
    )
    build_parser = command("build", build, "Construye imágenes OCI")
    build_parser.add_argument("component", help="nombre del componente o 'all'")
    build_parser.add_argument("--tag", default="local")
    build_parser.add_argument("--version", default="0.0.0-local")
    build_parser.add_argument("--revision", default="")
    run_parser = command("run", serve_local, "Ejecuta un componente en el host con recarga")
    run_parser.add_argument("component")
    run_parser.add_argument("--port", type=int, default=8000)
    smoke_parser = command("smoke", smoke, "Smoke HTTP de salud")
    smoke_parser.add_argument("--base-url", required=True)
    smoke_parser.add_argument("--retries", type=int, default=10)
    smoke_parser.add_argument("--interval", type=float, default=3.0)
    command("db-up", db_up, "Inicia PostgreSQL efímero para pruebas")
    command("db-down", db_down, "Detiene el PostgreSQL efímero de pruebas")
    command("pubsub-up", pubsub_up, "Inicia el emulador de Pub/Sub para pruebas")
    command("pubsub-down", pubsub_down, "Detiene el emulador de Pub/Sub de pruebas")
    command(
        "k8s-secrets", k8s_secrets, "Genera contraseñas y clave de firma locales para Kubernetes"
    )
    render_parser = command("k8s-render", k8s_render, "Renderiza base y el overlay local")
    render_parser.add_argument("--use-example", action="store_true")
    render_parser.add_argument(
        "--engine",
        choices=("kubectl", "docker"),
        default="kubectl",
        help="docker: kustomize fijado por digest (CI); kubectl: el instalado (local)",
    )
    command("ci-fast", ci_fast, "Calidad + unitarias (push / pre-commit)")
    command("ci-full", ci_full, "Puerta completa de pull request")

    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
