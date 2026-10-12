# syntax=docker/dockerfile:1.7
# Imagen única parametrizada por componente. Construir desde la raíz del repositorio:
#   uv run python scripts/task.py build <componente>
# Las imágenes base están fijadas por digest; se actualizan mediante PR revisado.

ARG PYTHON_IMAGE=python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY bff ./bff
RUN test -n "${PACKAGE}" \
    && uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM ${PYTHON_IMAGE} AS runtime
ARG APP_FACTORY
ARG SERVICE_VERSION=0.0.0-local
RUN test -n "${APP_FACTORY}" \
    && groupadd --system --gid 10001 sportapp \
    && useradd --system --uid 10001 --gid sportapp --no-create-home --shell /usr/sbin/nologin sportapp
COPY --from=builder /opt/venv /opt/venv
# Contrato versionado de permisos por actor (ADR-025): los BFF y servicios lo leen al arrancar.
COPY contracts/authorization /opt/sportapp/contracts/authorization
ENV PATH=/opt/venv/bin:${PATH} \
    ACTOR_PERMISSIONS_PATH=/opt/sportapp/contracts/authorization/actor-permissions.v1.json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_FACTORY=${APP_FACTORY} \
    SERVICE_VERSION=${SERVICE_VERSION} \
    HTTP_PORT=8000
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "sportapp_platform.serve"]
