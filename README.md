# SportApp backend

Repositorio ejecutable del backend de SportApp: microservicios FastAPI, BFF web y móvil, contratos, pruebas, imágenes, manifiestos Kubernetes locales e integración continua.

La arquitectura y las decisiones viven en la documentación central (`documentacion/arquitectura`), no en este repositorio. Ante cualquier diferencia prevalece esa documentación.

> ¿Primera vez en este repositorio? Siga [Entorno local paso a paso](#entorno-local-paso-a-paso).

## Tabla de contenido

- [Estado](#estado)
- [Stack fijado](#stack-fijado)
- [Estructura](#estructura)
- [Comandos](#comandos)
- [Configuración](#configuración)
- [Entorno local paso a paso](#entorno-local-paso-a-paso)
  - [Resumen rápido](#resumen-rápido)
  - [Lista de control](#lista-de-control)
  - [1. Qué se va a construir](#1-qué-se-va-a-construir)
  - [2. Antes de empezar: terminal y carpeta](#2-antes-de-empezar-terminal-y-carpeta)
  - [3. Verificar Python](#3-verificar-python)
  - [4. Verificar Docker Desktop y WSL 2](#4-verificar-docker-desktop-y-wsl-2)
  - [5. Instalar uv y dejarlo en el PATH](#5-instalar-uv-y-dejarlo-en-el-path)
  - [6. Instalar las dependencias del proyecto](#6-instalar-las-dependencias-del-proyecto)
  - [7. Activar Kubernetes en Docker Desktop](#7-activar-kubernetes-en-docker-desktop)
  - [8. Verificar el clúster y entender los contextos](#8-verificar-el-clúster-y-entender-los-contextos)
  - [9. Generar las contraseñas locales](#9-generar-las-contraseñas-locales)
  - [10. Construir las imágenes](#10-construir-las-imágenes)
  - [11. Desplegar y seguir el arranque](#11-desplegar-y-seguir-el-arranque)
  - [12. Usar los servicios: documentación y prueba de HU001](#12-usar-los-servicios-documentación-y-prueba-de-hu001)
  - [13. Recolectar diagnóstico para pedir ayuda](#13-recolectar-diagnóstico-para-pedir-ayuda)
  - [14. Detener, reiniciar y limpiar](#14-detener-reiniciar-y-limpiar)
  - [15. Glosario](#15-glosario)
  - [Estado de verificación de esta guía](#estado-de-verificación-de-esta-guía)
- [Integración continua](#integración-continua)
- [Documentación relacionada](#documentación-relacionada)
- [Pendiente de decisión (no implementado)](#pendiente-de-decisión-no-implementado)

## Estado

Todos los componentes exponen `/health/live` y `/health/ready`. Historias implementadas en backend:

| HU | Operación | Componentes | Contrato |
| --- | --- | --- | --- |
| HU001 Registrar usuario | `POST /v1/users` (público, `Idempotency-Key` obligatorio); interno: `GET /v1/subscriptions/by-user/{user_id}/capabilities` en Afiliación | `bff_web` → `users_management` → `user_account` + `outbox_event` → `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → consumidor del alta gratuita de `affiliation_management` → `subscription` `FREE` + `outbox_event` → `afiliacion-outbox-publisher` → `sportapp.afiliacion.v1` | OpenAPI de `users-management`, `bff-web` y `affiliation-management` (0.2.0); eventos `usuario.registrado.v1` y `suscripcion.activada.v1` |
| HU003 Iniciar sesión | `POST /v1/sessions` y `POST /v1/sessions/refresh` (públicos); `GET /v1/me/capabilities` (con access token) en ambos BFF; `GET /.well-known/jwks.json` solo en el servicio | `bff_web` y `bff_mobile` → `users_management` → `refresh_token` + `login_failure_window`; para capacidades, BFF → JWKS de Usuarios + `actor-permissions.v1` + Afiliación (solo `athlete`) | OpenAPI 0.3.0 de `users-management`, 0.5.0 de `bff-web` (refresh en cookie `HttpOnly`) y 0.3.0 de `bff-mobile`; contrato `contracts/authorization/actor-permissions.v1.json`; sin eventos |

Detalles de HU001:

- **Correo:** se normaliza (minúsculas, sin espacios exteriores) y es único.
- **Contraseña:** 12–128 caracteres, sin reglas de composición; se guarda solo como hash Argon2id (m=19 MiB, t=2, p=1).
- **`actor_type`:** `athlete`, `organizer` o `service_provider`.
- **Políticas:** `accepted_policies_version` debe coincidir con `REGISTRATION_POLICIES_VERSION`.
- **Idempotencia:** un reintento con la misma clave y el mismo cuerpo, durante 24 h, devuelve el mismo `201` con `Idempotent-Replayed: true`.
- **Alta gratuita (ADR-024, criterios 6-9):** Afiliación consume `usuario.registrado.v1` (suscripción `sportapp.afiliacion.alta-gratuita.v1`) y, solo para `athlete`, crea una suscripción `FREE` activa con origen `AUTO_REGISTRATION`. `organizer` y `service_provider` no reciben suscripción. Cada evento queda en `inbox_event`; una entrega repetida no crea otra suscripción y un índice único parcial impide dos suscripciones activas por usuario. Inbox, suscripción y outbox se confirman juntos y el mensaje se confirma (ACK) después del COMMIT; un mensaje inválido no se confirma y, tras 10 intentos, va a la DLQ `sportapp.afiliacion.alta-gratuita.dlq`. La activación publica `suscripcion.activada.v1` en `sportapp.afiliacion.v1` con `user_id` como clave de orden.
- **Catálogo:** la migración `0002_free_plan_catalog` precarga `plans-2026-10-11.v1`: `FREE` (11 capacidades), `INTERMEDIATE` (14) y `PREMIUM` (16). Los planes pagos quedan definidos pero no seleccionables, sin precio hasta que se aprueben los datos comerciales. Afiliación no queda lista (`/health/ready` 503) si falta esa versión, un plan o una capacidad.
- **Capacidades (interno):** `GET /v1/subscriptions/by-user/{user_id}/capabilities` devuelve `ACTIVE` con plan, versión, catálogo y capacidades, o `NONE` sin capacidades: nunca concede `FREE` por suposición. Lo consumen los BFF para componer `GET /v1/me/capabilities` (HU003).
- **Errores:**

| Código | Estado | Origen |
| --- | --- | --- |
| `email_already_registered` | 409 | servicio |
| `idempotency_key_reused` | 409 | servicio |
| `validation_error` | 422 | servicio |
| `policies_version_mismatch` | 422 | servicio |
| `upstream_unavailable` | 503 | BFF |
| `upstream_timeout` | 504 | BFF |

Detalles de HU003 (decisiones aprobadas el 2026-10-10; cookie del canal web aprobada el 2026-10-11):

- **Servicio y `bff_mobile`:** `200` con `token_type` (`Bearer`), `access_token`, `expires_in`, `refresh_token`, `refresh_expires_in` y `user` (`user_id`, `full_name`, `email`, `actor_type`), con `Cache-Control: no-store`. `/refresh` recibe `{"refresh_token": "..."}`.
- **`bff_web` (contrato 0.4.0):** el refresh token **no** viaja en el cuerpo. Se entrega en la cookie `__Secure-sportapp_refresh` (`HttpOnly; Secure; Path=/v1/sessions; SameSite=<REFRESH_COOKIE_SAME_SITE>; Max-Age=<refresh_expires_in>`). `POST /v1/sessions/refresh` va sin cuerpo: lee la cookie, la rota y devuelve un access token nuevo; con `401 invalid_refresh_token` borra la cookie. Un fallo temporal del servicio (`503`/`504`) no la borra.
- **CSRF en `bff_web`:** iniciar y renovar sesión exigen un `Origin` de `CORS_ALLOWED_ORIGINS`; si falta o no está, `403 origin_not_allowed` sin llamar al servicio.
- **Access token:** JWT EdDSA (Ed25519) de 15 min con `iss`, `aud`, `sub`, `iat`, `nbf`, `exp`, `jti` y `actor_type`; el encabezado lleva `kid` (huella RFC 7638). Otro servicio lo valida con `sportapp_platform.tokens.AccessTokenVerifier` y el JWKS de `users-management` (`GET /.well-known/jwks.json`).
- **Refresh token:** opaco, 7 días, guardado solo como hash SHA-256. Rota en cada uso; reutilizar uno ya rotado revoca toda la familia (la sesión).
- **Sin enumeración:** correo inexistente, contraseña incorrecta y cuenta `disabled` reciben el mismo `401 invalid_credentials`; un correo inexistente también se verifica contra un hash Argon2id ficticio.
- **Control de abuso:** más de 5 fallos en 15 min para el mismo correo + IP → `429 too_many_attempts` con `Retry-After` hasta que vence la ventana. No bloquea la cuenta. El BFF envía la IP del usuario en `X-Client-IP`.
- **Auditoría:** `audit.user_login` y `audit.session_refresh` con resultado e identificadores, nunca correo, contraseña ni tokens.
- **Navegación por actor (ADR-024/025, criterios 7-10):** `GET /v1/me/capabilities` en `bff-web` y `bff-mobile`, con `Authorization: Bearer <access token>`. El BFF valida el token con el JWKS de `users-management` (en memoria, recargado cada 5 min o ante un `kid` nuevo), toma `user_id` y `actor_type` del token (el cliente no puede consultar a otro) y responde `actor_type`, `subscription_status`, plan, `catalog_version`, `authorization_policy_version`, `permissions` y `capabilities`, con `Cache-Control: no-store`:
  - `athlete`: capacidades de su suscripción en Afiliación (`ACTIVE`) o `PROVISIONING` sin capacidades; nunca `FREE` por suposición.
  - `organizer` y `service_provider`: `NOT_APPLICABLE` y sus permisos de `actor-permissions.v1` habilitados en el canal (web: Crear evento; Crear y Publicar servicios; móvil: las sugerencias de HU026-HU030).
  - Sin token o token inválido: `401 invalid_access_token`; actor ausente o desconocido: `403 unsupported_actor_type`; Afiliación o el JWKS no disponibles: `503 upstream_unavailable`.
- **Errores:**

| Código | Estado | Origen |
| --- | --- | --- |
| `invalid_credentials` | 401 | servicio |
| `invalid_refresh_token` | 401 | servicio / BFF web (sin cookie o cookie inválida) |
| `origin_not_allowed` | 403 | BFF web |
| `invalid_access_token` | 401 | BFF (capacidades) |
| `unsupported_actor_type` | 403 | BFF (capacidades) |
| `too_many_attempts` | 429 | servicio |
| `validation_error` | 422 | servicio / BFF |
| `upstream_unavailable` | 503 | BFF |
| `upstream_timeout` | 504 | BFF |

> `SameSite`: con `strict` (por defecto) la cookie solo viaja si la web y el BFF comparten sitio. En local se cumple (`localhost` con distinto puerto). En producción, `web` y `bff-web` en `*.run.app` son sitios distintos: hace falta un dominio propio común o un proxy por ruta (decisión de la plataforma, pendiente). `none` funciona entre sitios distintos en Chrome y Firefox, pero Safari bloquea la cookie.

## Stack fijado

| Elemento | Versión o decisión |
| --- | --- |
| Python | 3.13 (`.python-version`; imagen `python:3.13-slim` fijada por digest) |
| Gestor y lockfile | uv 0.12.23, workspace con un único `uv.lock` |
| Framework | FastAPI, Pydantic v2, pydantic-settings (versiones exactas en `uv.lock`) |
| Persistencia | SQLAlchemy 2, psycopg 3, PostgreSQL 17 |
| Migraciones | Alembic, un historial por base (escritura, lectura o única) |
| Servidor ASGI y cliente HTTP | Uvicorn (un proceso por pod) y HTTPX |
| Calidad | Ruff (formato y lint), mypy estricto |
| Pruebas | `unittest`, TestClient de FastAPI, coverage.py (umbral 80 %) |
| Seguridad | Bandit (SAST), pip-audit (dependencias), Gitleaks (secretos) |
| Rendimiento | k6 (`load/k6`) y Locust (`load/locust`, grupo `load`) |
| Kubernetes local | Docker Desktop, contexto `docker-desktop`, namespace `sportapp-local`, Kustomize |

## Estructura

```text
services/<dominio>/            microservicio: src/<paquete>/{domain,application,infrastructure,api}
  tests/{unit,integration,contract}
bff/web, bff/mobile            BFF por canal: src/<paquete>/{api,clients}; sin base de datos
libs/sportapp_platform/        solo capacidades técnicas: configuración, logs, correlación,
                               errores, salud, PostgreSQL, migraciones y utilidades de prueba
contracts/openapi/             OpenAPI versionado por componente
contracts/events/              JSON Schema de eventos aprobados (ninguno todavía)
deploy/docker/Dockerfile       imagen única parametrizada por componente, usuario no root
deploy/k8s/base, overlays/local  Kustomize: Deployments, Services, Jobs de migración, PostgreSQL local
load/                          k6 y Locust
scripts/task.py                comandos estables (local y CI)
.github/workflows/             ci.yml y deliver-development.yml
.githooks/pre-commit           validación rápida local
```

| Componente | Patrón | Bases |
| --- | --- | --- |
| `users_management` | CQRS | `usuarios_write`, `usuarios_read` |
| `affiliation_management` | Base única | `afiliacion` |
| `complementary_services` | Base única | `servicios` |
| `training_management` | CQRS | `entrenamientos_write`, `entrenamientos_read` |
| `events_management` | CQRS | `eventos_write`, `eventos_read` |
| `third_party_management` | Adaptador | ninguna hasta que el diseño la confirme |
| `bff_web`, `bff_mobile` | BFF | ninguna |

Reglas que verifican las pruebas de arquitectura (`tests/unit/test_architecture.py`):

- `domain` no importa frameworks, ORM, transporte ni otras capas;
- `application` no importa `infrastructure` ni `api`;
- un servicio no importa el código de otro servicio ni de un BFF;
- un BFF no importa servicios ni librerías de persistencia.

En los dominios CQRS, `/health/ready` solo depende de la base de escritura. Si cae la de lectura, los comandos continúan.

## Comandos

Todos se ejecutan desde la raíz del repositorio. CI invoca exactamente estos mismos comandos.

| Propósito | Comando |
| --- | --- |
| Instalar de forma reproducible | `uv sync --frozen` |
| Verificar lockfile | `uv run python scripts/task.py lock-check` |
| Aplicar formato | `uv run python scripts/task.py format` |
| Verificar formato / lint / tipos | `uv run python scripts/task.py format-check` · `lint` · `typecheck` |
| Pruebas unitarias | `uv run python scripts/task.py test unit` |
| PostgreSQL efímero para integración | `uv run python scripts/task.py db-up` (y `db-down`) |
| Emulador de Pub/Sub para integración | `uv run python scripts/task.py pubsub-up` (y `pubsub-down`); exporta `PUBSUB_EMULATOR_HOST=127.0.0.1:58085` |
| Pruebas de integración | `TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/postgres uv run python scripts/task.py test integration` |
| Pruebas de contrato | `uv run python scripts/task.py test contract` |
| Pruebas de componente (BFF → servicio → PostgreSQL) | `uv run python scripts/task.py test component` |
| Cobertura consolidada (80 %) | `uv run python scripts/task.py coverage` |
| Regenerar contratos OpenAPI | `uv run python scripts/task.py openapi-export` |
| SAST y dependencias | `uv run python scripts/task.py security` |
| Secretos | `uv run python scripts/task.py secrets` |
| Validación rápida (push / pre-commit) | `uv run python scripts/task.py ci-fast` |
| Puerta completa de PR | `uv run python scripts/task.py ci-full` (requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`) |
| Construir imagen | `uv run python scripts/task.py build <componente\|all>` |
| Ejecutar en el host con recarga | `uv run python scripts/task.py run <componente>` (lee `.env`) |
| Smoke de salud | `uv run python scripts/task.py smoke --base-url http://127.0.0.1:8000` |
| Generar contraseñas y clave de firma locales de Kubernetes | `uv run python scripts/task.py k8s-secrets` |
| Renderizar Kubernetes local | `uv run python scripts/task.py k8s-render` |

En PowerShell, `TEST_DATABASE_URL` se define con `$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"`.

Hook local, una sola vez por clon: `git config core.hooksPath .githooks`.

Componentes: `users-management`, `affiliation-management`, `complementary-services`, `training-management`, `events-management`, `third-party-management`, `bff-web`, `bff-mobile`.

## Configuración

Cada componente valida su configuración al iniciar. Si falta una variable, falla nombrándola y sin imprimir su valor. Ver [.env.example](.env.example).

| Variable | Componentes | Descripción |
| --- | --- | --- |
| `APP_ENV` | todos | `local`, `test`, `development`, `staging` o `production` (obligatoria). En `production` no se exponen `/docs` ni `/openapi.json`. |
| `LOG_LEVEL` | todos | `DEBUG`, `INFO`, `WARNING`, `ERROR` (por defecto `INFO`). |
| `HTTP_PORT` | todos | Puerto de escucha (por defecto 8000). |
| `SERVICE_VERSION` | todos | Versión que se registra en los logs (la imagen la fija al construirse). |
| `DATABASE_WRITE_URL`, `DATABASE_READ_URL` | users, training, events | `postgresql+psycopg://…` (obligatorias). |
| `DATABASE_URL` | affiliation, complementary services | `postgresql+psycopg://…` (obligatoria). |
| `REGISTRATION_POLICIES_VERSION` | users | Versión vigente de las políticas que se aceptan al registrarse (obligatoria). |
| `JWT_PRIVATE_KEY` | users | Clave privada Ed25519 en PEM que firma los access tokens (obligatoria; un valor inválido detiene el arranque). En `.env` va en una sola línea con `\n` literales. En Kubernetes se lee del Secret `sportapp-users-jwt`. |
| `JWT_ISSUER`, `JWT_AUDIENCE` | users | `iss` y `aud` de los access tokens (por defecto `sportapp-users-management` y `sportapp`). |
| `ACCESS_TOKEN_TTL_SECONDS`, `REFRESH_TOKEN_TTL_SECONDS` | users | Vigencia del access token (900) y del refresh token (604800). |
| `LOGIN_MAX_FAILURES`, `LOGIN_FAILURE_WINDOW_SECONDS` | users | Control de abuso por correo + IP: fallos permitidos (5) y ventana (900 s). |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC` | publicador de usuarios | Proyecto y tópico (por defecto `sportapp.usuarios.v1`). |
| `PUBSUB_EMULATOR_HOST` | publicador de usuarios | Solo local/CI: dirige el cliente al emulador. |
| `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `WORKER_ID` | publicadores de usuarios y afiliación | Lote (100), arrendamiento (30 s) e identidad de la réplica (valores iniciales de CMP-001). |
| `AFFILIATION_EXPECTED_CATALOG_VERSION` | affiliation | Versión de catálogo que debe estar instalada para quedar listo (obligatoria; hoy `plans-2026-10-11.v1`). Cambia en el mismo release que una migración de catálogo nueva. |
| `PUBSUB_PROJECT_ID`, `FREE_PLAN_SUBSCRIPTION`, `CONSUMER_MAX_MESSAGES` | consumidor del alta gratuita | Proyecto, suscripción (por defecto `sportapp.afiliacion.alta-gratuita.v1`) y mensajes simultáneos por réplica (10). |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC` | publicador de afiliación | Proyecto y tópico (por defecto `sportapp.afiliacion.v1`). |
| `USERS_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS` | bff-web, bff-mobile | URL del servicio de usuarios (obligatoria) y timeout (5 s). |
| `CORS_ALLOWED_ORIGINS` | bff-web | Orígenes del cliente web autorizados por CORS, separados por comas, con la forma `http(s)://host[:puerto]` y sin `/` final (por ejemplo `http://localhost:18082,http://localhost:4200`). Vacía o ausente: se rechaza toda llamada de otro origen. No admite comodines; un valor inválido detiene el arranque. Son también los únicos `Origin` aceptados para iniciar y renovar sesión (CSRF). |
| `REFRESH_COOKIE_SAME_SITE` | bff-web | `SameSite` de la cookie del refresh token: `strict` (por defecto), `lax` o `none`. Ver la nota de HU003 en [Estado](#estado). |
| `AFFILIATION_MANAGEMENT_BASE_URL` | bff-web, bff-mobile | URL del contrato interno de Afiliación para `GET /v1/me/capabilities` (obligatoria; en el clúster, `http://affiliation-management`). |
| `JWT_ISSUER`, `JWT_AUDIENCE` | bff-web, bff-mobile | `iss` y `aud` esperados en el access token (por defecto los mismos que emite `users-management`). |
| `ACTOR_PERMISSIONS_PATH` | bff-web, bff-mobile | Ruta del contrato `actor-permissions.v1.json`. La imagen la fija en `/opt/sportapp/contracts/authorization/`; en el repositorio se localiza sola. Un contrato ausente o inválido detiene el arranque. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_CONNECT_TIMEOUT_SECONDS` | servicios con base | Límites del pool (5, 5 y 5 por defecto). |
| `APP_FACTORY` | imagen | Fábrica `modulo:funcion`; la fija el Dockerfile. |

Para ejecutar `users-management` en el host (`task.py run`), genere una clave local en una sola línea y péguela como valor de `JWT_PRIVATE_KEY` en `.env` (no la comparta ni la reutilice en otro ambiente):

```powershell
uv run python -c "import sys; sys.path.insert(0, 'scripts'); from components import new_signing_key_pem; print(new_signing_key_pem().strip().replace(chr(10), chr(92) + 'n'))"
```

CORS en `bff-web` (la web corre en otro origen, en local y en Cloud Run): permite `GET` y `POST`, acepta las cabeceras `Authorization`, `Content-Type`, `Idempotency-Key` y `X-Correlation-ID`, expone al navegador `Location`, `Idempotent-Replayed`, `Retry-After` y `X-Correlation-ID`, y admite credenciales (`Access-Control-Allow-Credentials: true`) para que el navegador envíe la cookie del refresh token; por eso solo acepta orígenes explícitos. En Kubernetes las variables se leen de las claves opcionales `CORS_ALLOWED_ORIGINS` y `REFRESH_COOKIE_SAME_SITE` del ConfigMap `sportapp-runtime`.

Observabilidad incluida:
- Logs JSON con `service`, `environment`, `version` y `correlation_id`.
- Un registro `request.completed` por solicitud, con método, ruta, estado y latencia.
- `X-Correlation-ID` se acepta (solo si es un UUID) y siempre se devuelve.
- Errores con el formato común (propuesto) `code`, `message`, `correlation_id` y `details`.

## Entorno local paso a paso

Esta guía lleva desde un equipo Windows sin nada configurado hasta tener todo el backend corriendo en el **Kubernetes integrado en Docker Desktop**, la plataforma local definida por ADR-018:

- contexto `docker-desktop`;
- namespace `sportapp-local`;
- manifiestos Kustomize en `deploy/k8s/overlays/local`.

Al final podrá abrir la documentación OpenAPI en el navegador y probar el registro de usuarios (HU001).

> **Regla de seguridad:** todos los comandos indican `--context docker-desktop --namespace sportapp-local`. El equipo puede tener acceso a clústeres GKE, y un `kubectl` sin contexto explícito podría modificarlos.

Cada paso indica:

- **Para qué sirve.**
- **Qué ejecutar.** Comandos para **PowerShell**, uno por bloque.
- **Qué debe ver.** El resultado esperado.
- **Si no ve eso.** La causa probable y la solución.

No avance al siguiente paso hasta que el actual muestre el resultado esperado. Si algo falla y no está en esta guía, vaya a la [sección 13](#13-recolectar-diagnóstico-para-pedir-ayuda) y comparta esa salida.

### Resumen rápido

Para quien ya hizo la configuración inicial (secciones 2 a 6). La columna *Sección* indica dónde está el detalle de cada paso.

| Sección | Qué hacer | Frecuencia |
| --- | --- | --- |
| [7](#7-activar-kubernetes-en-docker-desktop) | Activar Kubernetes en Docker Desktop (interfaz gráfica) | Una vez |
| [8](#8-verificar-el-clúster-y-entender-los-contextos) | `kubectl --context docker-desktop get nodes` → nodo `Ready` | Una vez |
| [9](#9-generar-las-contraseñas-locales) | `uv run python scripts/task.py k8s-secrets` | Una vez |
| [10](#10-construir-las-imágenes) | `uv run python scripts/task.py build all` | Cada vez que cambie el código |
| [11](#11-desplegar-y-seguir-el-arranque) | `kubectl --context docker-desktop --namespace sportapp-local apply --kustomize deploy/k8s/overlays/local` | Cada despliegue |
| [11.5](#115-estado-final-esperado) | `kubectl --context docker-desktop --namespace sportapp-local get pods` → todo `Running`/`Completed` | Cada despliegue |
| [12](#12-usar-los-servicios-documentación-y-prueba-de-hu001) | `port-forward` y abrir `http://127.0.0.1:8000/docs` | Cuando quiera usar los servicios |

### Lista de control

Marque cada casilla al terminar el paso. Así sabe en qué punto está si se interrumpe.

- [ ] 3. `py -3.13 --version` → `Python 3.13.x`
- [ ] 4. `docker info` responde y Docker Desktop está en verde
- [ ] 5. `uv --version` → `uv 0.12.23`
- [ ] 6. `uv sync --frozen` termina sin errores y `ci-fast` termina con `OK` en todas las suites
- [ ] 7. Kubernetes en verde en Docker Desktop
- [ ] 8. `kubectl --context docker-desktop get nodes` → `Ready`
- [ ] 9. Existe `deploy\k8s\overlays\local\secrets.local.env`
- [ ] 10. Hay 8 imágenes `sportapp/*:local`
- [ ] 11. Todos los pods en `Running` o `Completed`
- [ ] 12. `http://127.0.0.1:8000/docs` abre en el navegador

### 1. Qué se va a construir

```text
 Navegador (Swagger /docs)
        │  port-forward 8000
        ▼
 ┌──────────────────────── Kubernetes de Docker Desktop (namespace sportapp-local) ───────────────────────┐
 │                                                                                                        │
 │  bff-web ──► users-management ──► postgres (bases usuarios_write / usuarios_read y las de otros dominios)│
 │  bff-mobile                         ▲                                                                   │
 │  affiliation-management ────────────┤                                                                   │
 │  complementary-services ────────────┤                                                                   │
 │  training-management ───────────────┤                                                                   │
 │  events-management ─────────────────┘                                                                   │
 │  third-party-management                                                                                 │
 │                                                                                                        │
 │  users-management-outbox-publisher ──► pubsub-emulator (tópico sportapp.usuarios.v1)                   │
 │                                                                                                        │
 │  Jobs que se ejecutan una vez: 8 migraciones de base de datos + creación del tópico                    │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Inventario exacto de lo que se crea (34 recursos):

| Tipo | Cantidad | Nombres |
| --- | ---: | --- |
| Namespace | 1 | `sportapp-local` |
| StatefulSet | 1 | `postgres` |
| Deployment | 10 | `bff-web`, `bff-mobile`, `users-management`, `affiliation-management`, `complementary-services`, `training-management`, `events-management`, `third-party-management`, `users-management-outbox-publisher`, `pubsub-emulator` |
| Job | 9 | `users-management-migrate-write`, `users-management-migrate-read`, `affiliation-management-migrate-main`, `complementary-services-migrate-main`, `training-management-migrate-write`, `training-management-migrate-read`, `events-management-migrate-write`, `events-management-migrate-read`, `users-management-pubsub-setup` |
| Service | 10 | uno por Deployment de aplicación, más `postgres` y `pubsub-emulator` |
| ConfigMap | 2 | `sportapp-runtime-<hash>` (configuración) y `postgres-init-<hash>` (script que crea las bases) |
| Secret | 1 | `sportapp-database-<hash>` (contraseñas del paso 9) |

Tiempo estimado la primera vez: **20 a 40 minutos**. Gran parte es descarga de imágenes; solo la del emulador de Pub/Sub pesa unos 1,7 GB.

### 2. Antes de empezar: terminal y carpeta

**Para qué sirve:** todos los comandos suponen PowerShell abierta en la carpeta `backend` del proyecto.

1. Abra **PowerShell**. Sirve la terminal integrada de VS Code (menú *Terminal → New Terminal*, perfil *PowerShell*) o la aplicación *Windows PowerShell*.
2. Vaya a la carpeta del backend:

```powershell
cd "D:\04 - Mestría en Ingeniería de Software\08 - Ciclo 8\Proyecto final II\aplicacion_proyecto_final\backend"
```

3. Verifique que está en el lugar correcto:

```powershell
Test-Path .\scripts\task.py
```

**Qué debe ver:** `True`.

**Si ve `False`:** no está en la carpeta `backend`. Repita el `cd`; la ruta va entre comillas porque tiene espacios y tildes.

> **Importante:** use **PowerShell**, no *Git Bash* ni *CMD*. Los comandos de esta guía usan sintaxis de PowerShell.

### 3. Verificar Python

**Para qué sirve:** el backend usa Python 3.13, y uv se instala con él.

```powershell
py -3.13 --version
```

**Qué debe ver:** `Python 3.13.x` (por ejemplo `Python 3.13.2`).

| Si ve | Causa | Solución |
| --- | --- | --- |
| `No suitable Python runtime found` | Python 3.13 no está instalado | Instale Python 3.13 desde python.org, marcando *Add python.exe to PATH* y *py launcher*; luego abra una terminal nueva. |
| `py : The term 'py' is not recognized` | Falta el lanzador `py` | Reinstale Python 3.13 marcando *py launcher*. |

### 4. Verificar Docker Desktop y WSL 2

**Para qué sirve:** Docker Desktop construye las imágenes y aloja el clúster Kubernetes. En Windows funciona sobre WSL 2, el subsistema de Linux.

#### 4.1 Docker Desktop abierto y en verde

1. Abra **Docker Desktop** desde el menú Inicio.
2. Espere a que el indicador inferior izquierdo diga **Engine running** (en verde).

#### 4.2 Docker responde desde la terminal

```powershell
docker version --format "Cliente {{.Client.Version}} / Servidor {{.Server.Version}}"
```

**Qué debe ver:** dos versiones, por ejemplo `Cliente 29.8.1 / Servidor 29.8.1`.

| Si ve | Causa | Solución |
| --- | --- | --- |
| `error during connect` o `The system cannot find the file specified` | Docker Desktop no está iniciado o aún está arrancando | Ábralo y espere *Engine running*. |
| `docker : The term 'docker' is not recognized` | Docker Desktop no está instalado o la terminal se abrió antes de instalarlo | Instálelo y abra una terminal nueva. |

#### 4.3 WSL 2 activo

```powershell
wsl --status
```

**Qué debe ver:** `Default Version: 2` (o `Versión predeterminada: 2`).

**Si dice versión 1 o WSL no está instalado:** ejecute `wsl --install` en una PowerShell **como administrador**, reinicie Windows y vuelva a abrir Docker Desktop.

#### 4.4 Memoria disponible para Docker

El entorno completo usa aproximadamente **4 a 6 GB de RAM**.

- **Con WSL 2** (lo normal), la memoria la decide Windows. Si el equipo tiene 8 GB o menos, cierre otras aplicaciones pesadas antes del paso 11.
- **Si Docker Desktop muestra límites** en *Settings (engranaje) → Resources → Advanced*, asigne al menos **4 CPU y 8 GB**, y pulse *Apply & restart*.

### 5. Instalar uv y dejarlo en el PATH

**Para qué sirve:** uv administra Python y las dependencias del proyecto, como npm en Angular o Gradle en Android. Todos los comandos del proyecto se ejecutan con `uv run ...`.

> ¿No conoce uv? El curso corto de uv está en la documentación central del proyecto, en `documentacion/formacion/curso-uv.md`.

#### 5.1 Instalar uv

```powershell
py -3.13 -m pip install --user uv==0.12.23
```

**Qué debe ver**, uno de estos dos:

- `Successfully installed uv-0.12.23`: se instaló ahora.
- `Requirement already satisfied: uv==0.12.23 ...`: ya estaba instalado; no hay que hacer nada más.

Un aviso sobre una versión nueva de pip se puede ignorar.

> `--user` instala uv para **su usuario de Windows**, en `C:\Users\<usuario>\AppData\Roaming\Python\Python313\Scripts\uv.exe`. **No** lo instala dentro del `.venv` del proyecto, y eso es lo correcto: uv es quien crea el `.venv` (paso 6).

#### 5.2 Comprobar si la carpeta de uv está en el PATH

Windows no busca programas en la carpeta anterior hasta que se agrega a la variable `Path`.

```powershell
[Environment]::GetEnvironmentVariable("Path", "User") -split ";" | Select-String -SimpleMatch "Python313\Scripts"
```

- **Muestra una línea** con `...\Roaming\Python\Python313\Scripts`: ya está; salte al **5.4**.
- **No muestra nada:** continúe con **5.3**.

#### 5.3 Agregar la carpeta al PATH (una sola vez)

Elija **una** opción.

**Opción A, por comando.** Pegue esta línea completa y pulse Enter **una sola vez**:

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:APPDATA\Python\Python313\Scripts", "User")
```

- **No muestra ningún mensaje.** Si no aparece texto en rojo, funcionó.
- **No la repita:** agregaría la carpeta dos veces.
- **Si PowerShell muestra `>>`:** la línea quedó incompleta al pegarla. Pulse `Ctrl+C` y pegue de nuevo la línea completa.

**Opción B, por la interfaz de Windows:**

1. Pulse la tecla Windows y escriba **variables de entorno**.
2. Abra **Editar las variables de entorno de esta cuenta**.
3. En la parte superior (**Variables de usuario**), seleccione **Path** y pulse **Editar...**.
4. Pulse **Nuevo** y pegue: `%APPDATA%\Python\Python313\Scripts`
5. Pulse **Aceptar** en las dos ventanas.

Después de cualquiera de las dos opciones, repita el comando de **5.2**: ahora debe mostrar la línea.

#### 5.4 Cerrar y volver a abrir la terminal

El cambio del PATH solo lo ven las terminales **nuevas**:

- **Terminal de VS Code:** cierre **VS Code completo** (todas sus ventanas) y vuelva a abrirlo. Abrir otra pestaña de terminal **no basta**.
- **PowerShell independiente:** cierre la ventana y abra otra.

Luego vuelva a la carpeta `backend` (paso 2).

#### 5.5 Verificar uv

```powershell
uv --version
```

**Qué debe ver:** `uv 0.12.23`.

| Si ve | Causa | Solución |
| --- | --- | --- |
| `uv : The term 'uv' is not recognized...` | La carpeta no está en el PATH o la terminal no se reabrió | Repita 5.2; si no muestra la línea, haga 5.3; luego 5.4. |
| Otra versión de uv | Hay otra instalación de uv con prioridad | Ejecute `Get-Command uv` para ver cuál se usa. Funciona igual si es 0.12.x; si no, reinstale la versión indicada. |

> **Alternativa temporal:** mientras resuelve el PATH, anteponga `py -3.13 -m` a cualquier comando `uv`. Por ejemplo, `py -3.13 -m uv run python scripts/task.py build all` funciona siempre.

### 6. Instalar las dependencias del proyecto

#### 6.1 Crear el entorno virtual

**Para qué sirve:** crea la carpeta `.venv` con las versiones exactas fijadas en `uv.lock`.

```powershell
uv sync --frozen
```

**Qué debe ver:** líneas como `Resolved ... packages` e `Installed ...`, o nada si ya estaba al día. No debe haber errores.

**Si falla `uv sync`** con errores de red o certificados: la red bloquea la descarga desde `pypi.org`. Pruebe con otra red o configure el proxy de su organización.

#### 6.2 Comprobar el proyecto con `ci-fast`

**Para qué sirve:** comprueba en menos de un minuto que el código de su copia está sano: dependencias coherentes, formato, estilo, tipos y pruebas unitarias. Hágalo **antes** de construir imágenes (paso 10). Un error que aquí se detecta en segundos le costaría 5 a 15 minutos de build más un despliegue para descubrirlo en Kubernetes.

```powershell
uv run python scripts/task.py ci-fast
```

**Qué debe ver:** tarda unos **30 a 60 segundos** (37 s en el equipo de desarrollo). La salida sigue este orden:

```text
$ ...uv.exe lock --check
Resolved 125 packages in 3ms
$ ...python.exe -m ruff format --check .
170 files already formatted
$ ...python.exe -m ruff check .
All checks passed!
$ ...python.exe -m mypy -p sportapp_platform -p users_management ... -p bff_mobile
Success: no issues found in 105 source files
$ ...python.exe -m mypy scripts
Success: no issues found in 2 source files
$ ...python.exe -m coverage run ... -m unittest discover -s libs\sportapp_platform\tests\unit ...
test_not_found_uses_common_format (...) ... ok
...
Ran 33 tests in 2.522s
OK
   (se repite "Ran N tests ... OK" para cada uno de los 9 paquetes)
```

- Las cantidades (`125 packages`, `170 files`, `33 tests`) crecen a medida que se agrega código; lo importante es que no haya errores.
- Las líneas JSON con `request.completed` o `outbox.published` en medio de las pruebas son **logs normales** de la aplicación, no errores.
- Al terminar bien, el comando vuelve al prompt sin mensajes en rojo. Puede confirmarlo con `$LASTEXITCODE`, que debe valer `0`.

#### 6.3 Qué hace `ci-fast` por dentro

##### El comando, palabra por palabra

| Parte | Qué hace |
| --- | --- |
| `uv run` | Ejecuta lo que sigue **dentro** del entorno `.venv` del proyecto. Antes revisa que `.venv` coincida con `uv.lock` y, si falta algo, lo instala. No hace falta activar el entorno a mano. |
| `python` | El Python 3.13 de `.venv`, no el Python global de Windows. |
| `scripts/task.py` | El **ejecutor de tareas** del repositorio. Reúne todos los comandos del proyecto (`build`, `test`, `lint`, `smoke`...) para que cualquier persona y el CI los ejecuten igual. |
| `ci-fast` | El nombre de la tarea. Es un **agregado**: no hace nada propio, solo ejecuta otras tareas en orden. |

En `scripts/task.py` la tarea es solo esto:

```python
def ci_fast(_):
    _sequence(lock_check, format_check, lint, typecheck)
    _sequence(test, suite="unit")
```

##### Los cinco pasos, en orden

Cada línea de la salida que empieza con `$ ` es el comando exacto que se está ejecutando. Puede copiarlo para repetir solo ese paso.

| # | Tarea | Herramienta | Qué verifica | Por qué importa |
| ---: | --- | --- | --- | --- |
| 1 | `lock-check` | `uv lock --check` | Que `uv.lock` corresponde exactamente a los `pyproject.toml` del workspace. | Si alguien agregó una dependencia y no regeneró el lock, las imágenes (que se construyen con `uv sync --frozen`) fallarían o usarían versiones distintas a las probadas. Va primero porque, si falla, todo lo demás estaría probando otro conjunto de librerías. |
| 2 | `format-check` | `ruff format --check .` | Que todos los `.py` tienen el formato acordado (líneas de máximo 100 caracteres, comillas, sangría). **No modifica archivos.** | Evita diferencias de formato en los commits y en las revisiones. |
| 3 | `lint` | `ruff check .` | Errores probables y malas prácticas: imports sin usar, variables sin definir, orden de imports, reglas de seguridad (`S`, equivalentes a Bandit), complejidad, nombres. Las reglas están en `[tool.ruff.lint]` del `pyproject.toml` raíz. | Atrapa errores antes de ejecutar el código. |
| 4 | `typecheck` | `mypy` en modo **estricto** | Que los tipos son coherentes en los 9 paquetes (`sportapp_platform`, 6 microservicios, 2 BFF) y en `scripts/`. Por ejemplo, que no se pase un `str` donde se espera un `UUID`, o que no se olvide manejar un `None`. | Python no revisa tipos al ejecutar; mypy encuentra esos errores sin necesidad de una prueba que los provoque. |
| 5 | `test unit` | `unittest` bajo `coverage` | Ejecuta la carpeta `tests/unit` de cada paquete por separado: primero la librería `libs/sportapp_platform`, luego los 6 servicios y los 2 BFF. Hoy son **117 pruebas** (33 de la librería, 45 de usuarios, 10 del BFF web y 29 entre los otros 6 componentes). | Las pruebas unitarias comprueban reglas de dominio, casos de uso y la API **sin infraestructura**: usan dobles en memoria en lugar de PostgreSQL o Pub/Sub. |

##### Cómo se comporta

- **Se detiene en el primer fallo.** Si el paso 3 falla, los pasos 4 y 5 no se ejecutan. Corrija y vuelva a ejecutar `ci-fast` completo.
- **No modifica su código.** Solo revisa. El único archivo que escribe son los datos de cobertura en `reports/coverage/`, una carpeta ignorada por Git.
- **No necesita Docker, PostgreSQL, Pub/Sub ni Kubernetes.** Funciona aunque Docker Desktop esté cerrado.
- **No aplica el umbral de cobertura del 80 %.** Mide la cobertura, pero el umbral lo exigen `ci-full` y la tarea `coverage`.
- **Código de salida:** `0` si todo pasa, distinto de `0` si algo falla. Así lo usan el hook de Git y el CI.

##### Dónde más se usa

| Lugar | Cómo lo usa |
| --- | --- |
| **Hook `pre-commit`** (`.githooks/pre-commit`) | Si activó los hooks con `git config core.hooksPath .githooks`, Git ejecuta `ci-fast` antes de cada `git commit` y **cancela el commit** si falla. |
| **CI en GitHub** (`.github/workflows/ci.yml`) | Los jobs `quality` y `unit` ejecutan los mismos pasos uno por uno. Además, el CI ejecuta dos cosas que `ci-fast` **no** incluye: `k8s-render --use-example` (valida los manifiestos de Kubernetes) y `secrets` (Gitleaks, busca credenciales). Por eso un `ci-fast` local en verde **no garantiza** que el CI quede en verde. |
| **`ci-full`** | Es la versión completa, equivalente a la puerta de un pull request. Ejecuta lo mismo que `ci-fast` y añade las pruebas de integración, contrato y componente (con PostgreSQL y el emulador de Pub/Sub reales), el umbral del 80 % y los análisis de seguridad (Bandit y pip-audit). Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`; vea [Comandos](#comandos). |

##### Ejecutar un solo paso

Útil para repetir solo lo que falló:

```powershell
uv run python scripts/task.py lock-check
```

```powershell
uv run python scripts/task.py format-check
```

```powershell
uv run python scripts/task.py lint
```

```powershell
uv run python scripts/task.py typecheck
```

```powershell
uv run python scripts/task.py test unit
```

##### Si `ci-fast` falla

Busque el **último** `$ ...` antes del error: indica qué paso falló.

| Si ve | Paso | Causa | Solución |
| --- | --- | --- | --- |
| `The lockfile at uv.lock needs to be updated` | 1 | Se cambió un `pyproject.toml` sin regenerar `uv.lock` | Quien cambió las dependencias debe ejecutar `uv lock` y versionar `uv.lock` junto con el `pyproject.toml`. Si usted no tocó dependencias, actualice su copia del repositorio. |
| `Would reformat: <archivo>` y `N files would be reformatted` | 2 | Hay archivos sin el formato acordado | `uv run python scripts/task.py format`, que **sí** modifica los archivos. Revise los cambios y repita `ci-fast`. |
| `<archivo>:<línea>:<col>: <CÓDIGO> <mensaje>` y `Found N errors` | 3 | Ruff encontró un problema; el código (por ejemplo `F401`, `S105`) identifica la regla | `format` corrige algunos automáticamente. Los demás se corrigen a mano en la línea indicada. |
| `<archivo>:<línea>: error: ... [<código>]` y `Found N errors in M files` | 4 | Error de tipos | Corrija el tipo en la línea indicada. No use `# type: ignore` para silenciarlo. |
| `FAIL: test_...` o `ERROR: test_...` y al final `FAILED (failures=N)` | 5 | Una prueba unitaria no pasa | El bloque `Traceback` encima indica el archivo y la aserción. Corrija el código, o la prueba si el cambio de comportamiento es intencional. No desactive pruebas. |
| `uv : The term 'uv' is not recognized` | — | uv no está en el PATH | Paso 5, o use `py -3.13 -m uv run python scripts/task.py ci-fast`. |
| `uv no está disponible; ejecute los comandos con uv run` o `No module named ruff` | — | Ejecutó `python scripts/task.py ...` sin `uv run`, con el Python global | Anteponga siempre `uv run`. |

### 7. Activar Kubernetes en Docker Desktop

**Para qué sirve:** crea un clúster Kubernetes de un nodo dentro de Docker Desktop, en el que se despliega todo el backend.

#### 7.1 Crear el clúster

La interfaz depende de la versión de Docker Desktop.

**Versiones recientes** (en el menú lateral hay un ítem **Kubernetes**):

1. Pulse **Kubernetes** en el menú lateral.
2. Si aparece *Enable Kubernetes* con el botón **Create cluster**, púlselo.
3. Elija el aprovisionador **kubeadm**.
4. Confirme con **Create** o **Apply**.

**Versiones anteriores:**

1. Pulse el engranaje (**Settings**) → **Kubernetes**.
2. Marque **Enable Kubernetes**.
3. Si pregunta el método de aprovisionamiento, elija **kubeadm**.
4. Pulse **Apply & restart**.

**Por qué kubeadm y no kind:**
- con kubeadm el clúster usa el mismo motor Docker del equipo, así que las imágenes que construya en el paso 10 se usan directamente;
- con kind habría que cargar cada imagen en el clúster por separado.

#### 7.2 Esperar a que quede en verde

La primera vez Docker Desktop descarga las imágenes del clúster, lo que puede tardar entre **2 y 10 minutos**. Termina cuando:

- el ítem Kubernetes muestra el estado **running** en verde, o
- en la barra inferior aparece **Kubernetes running**.

| Si ve | Causa | Solución |
| --- | --- | --- |
| *Kubernetes is starting...* durante más de 15 minutos | Descarga lenta o bloqueada | Revise la conexión. En *Troubleshoot* (ícono de bicho) → *Restart*. |
| *Kubernetes failed to start* | Estado corrupto del clúster | *Settings → Kubernetes → Reset Kubernetes cluster*. Es seguro en este punto porque aún no hay nada desplegado. |

### 8. Verificar el clúster y entender los contextos

#### 8.1 ¿Qué es un "contexto"?

`kubectl` es la herramienta de línea de comandos de Kubernetes y puede conocer **varios clústeres**. Cada clúster conocido es un **contexto**. Todos se guardan en el archivo `C:\Users\<usuario>\.kube\config`.

```powershell
kubectl config get-contexts
```

**Qué debe ver**, algo como:

```text
CURRENT   NAME
          docker-desktop
*         gke_proyecto-final-miso-416801_us-east1_cluster-proyecto-final
          gke_devnativa24_us-central1_cluster-devnativa-24
```

Cómo leerlo:

| Elemento | Significado |
| --- | --- |
| `docker-desktop` | **El clúster local** creado en el paso 7. **Debe aparecer.** |
| `gke_...` | Clústeres en Google Cloud de otros cursos o proyectos. Esta guía **no** los toca. |
| `*` en la columna `CURRENT` | El contexto **activo**: el que usa `kubectl` cuando no se le indica uno. |

Que haya varios clústeres es normal. Que el `*` esté en un `gke_...` **no es un problema**, porque todos los comandos de esta guía incluyen `--context docker-desktop`, que obliga a usar el clúster local.

**Si `docker-desktop` no aparece:** Kubernetes no se activó; vuelva al paso 7.

#### 8.2 Verificar el clúster local (solo lectura)

```powershell
kubectl --context docker-desktop get nodes
```

**Qué debe ver:**

```text
NAME             STATUS   ROLES           AGE   VERSION
docker-desktop   Ready    control-plane   5m    v1.36.1
```

Lo importante es `STATUS = Ready`. La versión puede variar.

```powershell
kubectl --context docker-desktop get storageclass
```

**Qué debe ver:** una línea `hostpath (default)`. Es el almacenamiento que usará PostgreSQL.

| Si ve | Causa | Solución |
| --- | --- | --- |
| `NotReady` | El clúster aún está arrancando | Espere 1 o 2 minutos y repita. |
| `Unable to connect to the server` | Kubernetes detenido | Revise que esté en verde en Docker Desktop (paso 7.2). |
| `context "docker-desktop" does not exist` | Kubernetes no está activado | Paso 7. |

#### 8.3 ¿Cambiar el contexto activo? (opcional)

- **Si trabajará sobre todo en local**, puede activar el contexto local:

  ```powershell
  kubectl config use-context docker-desktop
  ```

- **Si alterna con GKE**, no cambie nada y use siempre `--context docker-desktop`.

Para saber cuál está activo: `kubectl config current-context`.

### 9. Generar las contraseñas locales

**Para qué sirve:** PostgreSQL local necesita una contraseña de administrador y una por dominio, y `users-management` necesita una clave Ed25519 para firmar los access tokens (HU003). Se guardan en archivos que **no se suben a Git**.

```powershell
uv run python scripts/task.py k8s-secrets
```

**Qué debe ver:** `Creado deploy\k8s\overlays\local\secrets.local.env con 6 contraseñas locales.` y `Creado deploy\k8s\overlays\local\jwt-private-key.local.pem (clave de firma Ed25519 local).`

- Las contraseñas y la clave son aleatorias y **no se muestran** en pantalla.
- El comando crea solo los archivos que faltan. Si ya hizo este paso antes de HU003, verá únicamente la línea de la clave.
- Si ambos archivos existen, responde `Los secretos locales ya existen; no se sobrescriben.` Eso es correcto.
- Borrar o regenerar la clave invalida las sesiones abiertas, pero no afecta los datos.

Compruebe que el archivo existe:

```powershell
Test-Path .\deploy\k8s\overlays\local\secrets.local.env
```

**Qué debe ver:** `True`.

> **Regla importante:** PostgreSQL guarda las contraseñas la **primera vez** que arranca. Si después borra o regenera este archivo, los servicios ya no podrán conectarse hasta que borre también el volumen de datos. Vea la [sección 14.4](#144-empezar-de-cero-borra-los-datos-locales).

### 10. Construir las imágenes

**Para qué sirve:** genera una imagen Docker por cada componente: 6 microservicios y 2 BFF.

```powershell
uv run python scripts/task.py build all
```

- **Tiempo:** la primera vez, **5 a 15 minutos** (descarga Python base y librerías). Las siguientes veces, mucho menos.
- **Qué debe ver:** al final de cada componente aparece `naming to docker.io/sportapp/<componente>:local`. El comando termina sin `ERROR`.

Verifique las 8 imágenes:

```powershell
docker images --filter "reference=sportapp/*" --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
```

**Qué debe ver** (el tamaño puede variar):

```text
sportapp/bff-web:local                 278MB
sportapp/users-management:local        356MB
sportapp/bff-mobile:local              273MB
sportapp/third-party-management:local  273MB
sportapp/events-management:local       273MB
sportapp/training-management:local     273MB
sportapp/complementary-services:local  273MB
sportapp/affiliation-management:local  273MB
```

| Si ve | Causa | Solución |
| --- | --- | --- |
| `ERROR: failed to solve ... failed to fetch` | Sin acceso a Docker Hub o `ghcr.io` | Revise la conexión e intente de nuevo. |
| `no space left on device` | Disco de Docker lleno | `docker system df` muestra el uso; libere espacio con `docker image prune`. |
| Faltan imágenes en la lista | Se interrumpió el build | Construya solo la que falta: `uv run python scripts/task.py build <componente>`. |

### 11. Desplegar y seguir el arranque

#### 11.1 Aplicar los manifiestos

```powershell
kubectl --context docker-desktop --namespace sportapp-local apply --kustomize deploy/k8s/overlays/local
```

**Qué debe ver:** unas 34 líneas terminadas en `created`. Por ejemplo `namespace/sportapp-local created`, `deployment.apps/users-management created` o `job.batch/users-management-migrate-write created`.

| Si ve | Causa | Solución |
| --- | --- | --- |
| `Error: ... secrets.local.env: no such file` | Falta el paso 9 | Haga el paso 9 y repita. |
| `field is immutable` en un `job.batch/...` | Re-aplicó después de un cambio y los Jobs ya existían | Vea la [sección 14.3](#143-repetir-las-migraciones). |
| `namespaces "sportapp-local" not found` en la primera ejecución | El namespace aún se estaba creando | Repita el mismo comando. |

#### 11.2 Qué ocurre durante el arranque (orden normal)

En los primeros minutos verá estados intermedios. **Es normal:**

1. **`postgres-0`** descarga su imagen e inicializa las bases. Tarda entre 30 segundos y 2 minutos.
2. **`pubsub-emulator`** descarga una imagen grande (~1,7 GB). Puede tardar **varios minutos** la primera vez.
3. **Jobs de migración** (`*-migrate-*`): si PostgreSQL aún no está listo, sus primeros intentos terminan en `Error`. Kubernetes reintenta solo, hasta 6 veces con espera creciente, y el último intento queda en **`Completed`**.
4. **`users-management-pubsub-setup`**: igual, reintenta hasta que el emulador responde y termina en `Completed`.
5. **Microservicios y BFF**: arrancan en seguida, pero muestran `READY 0/1` hasta que su base de datos responde. Luego pasan a `1/1`.

#### 11.3 Observar el progreso

Pegue este comando y **déjelo abierto**. Se actualiza solo; para salir, pulse `Ctrl+C`:

```powershell
kubectl --context docker-desktop --namespace sportapp-local get pods --watch
```

Para una foto puntual (sin `--watch`):

```powershell
kubectl --context docker-desktop --namespace sportapp-local get pods
```

#### 11.4 Esperar a que todo termine

Estos dos comandos esperan hasta 10 minutos y avisan cuando todo está listo:

```powershell
kubectl --context docker-desktop --namespace sportapp-local wait --for=condition=Complete job --all --timeout=600s
```

```powershell
kubectl --context docker-desktop --namespace sportapp-local wait --for=condition=Available deployment --all --timeout=600s
```

**Qué debe ver:** una línea `condition met` por cada Job (9) y por cada Deployment (10).

#### 11.5 Estado final esperado

```powershell
kubectl --context docker-desktop --namespace sportapp-local get pods
```

Debe ver:

- **11 pods `Running` con `READY 1/1`:** `postgres-0`, `pubsub-emulator-...`, `users-management-...`, `affiliation-management-...`, `complementary-services-...`, `training-management-...`, `events-management-...`, `third-party-management-...`, `bff-web-...`, `bff-mobile-...` y `users-management-outbox-publisher-...`.
- **Pods `Completed`** de los 9 Jobs.
- Pueden quedar además pods de Jobs en `Error`: son los intentos fallidos del principio. Mientras su Job tenga un pod `Completed`, está bien.

Resumen de Jobs (todos deben decir `Complete`):

```powershell
kubectl --context docker-desktop --namespace sportapp-local get jobs
```

#### 11.6 Problemas durante el arranque

Para cualquier pod con problemas, estos dos comandos dan el detalle. Reemplace `<pod>` por el nombre que muestra `get pods`:

```powershell
kubectl --context docker-desktop --namespace sportapp-local describe pod <pod>
```

```powershell
kubectl --context docker-desktop --namespace sportapp-local logs <pod>
```

En `describe`, lea la sección **Events** al final: ahí está la causa.

| Estado del pod | Causa probable | Solución |
| --- | --- | --- |
| `ErrImagePull` / `ImagePullBackOff` en un componente `sportapp/...` | La imagen no existe localmente o el clúster no es kubeadm | Paso 10 (verifique las 8 imágenes). Si el clúster es **kind**, cada imagen debe cargarse en el clúster: recree el clúster con kubeadm (paso 7). |
| `ImagePullBackOff` en `postgres` o `pubsub-emulator` | Sin acceso a internet para descargar la imagen | Revise la conexión; Kubernetes reintenta solo. |
| `ContainerCreating` durante muchos minutos en `pubsub-emulator` | Sigue descargando ~1,7 GB | Espere. `describe pod` muestra `Pulling image` en Events. |
| `Pending` en `postgres-0` | El volumen no se pudo crear | `kubectl --context docker-desktop get storageclass` debe mostrar `hostpath (default)`; si no, reinicie Kubernetes. |
| `CrashLoopBackOff` en `postgres-0` con `Permission denied` en los logs | El volumen no permite escribir al usuario de PostgreSQL | Comparta el diagnóstico de la [sección 13](#13-recolectar-diagnóstico-para-pedir-ayuda): requiere ajustar el manifiesto. |
| Job `*-migrate-*` agota los 6 intentos (`BackoffLimitExceeded`) | PostgreSQL no arrancó, o las contraseñas no coinciden con un volumen anterior | Revise `logs` del Job. Si dice `password authentication failed`, siga la [sección 14.4](#144-empezar-de-cero-borra-los-datos-locales). |
| Microservicio `Running` pero `READY 0/1` durante mucho tiempo | Su base no responde o falta su migración | `logs` del pod muestra `database.unavailable`; revise `postgres-0` y los Jobs de ese dominio. |
| `OOMKilled` | Falta memoria | Cierre aplicaciones o aumente la memoria (paso 4.4). |

### 12. Usar los servicios: documentación y prueba de HU001

Los servicios **no** quedan expuestos fuera del clúster. Para usarlos desde el navegador se abre un **reenvío de puerto** (`port-forward`).

#### 12.1 Abrir el reenvío de puertos

Abra una **terminal nueva** para cada comando y **déjela abierta**: el reenvío funciona solo mientras el comando esté corriendo.

Terminal 1, BFF web (la API que consumirá Angular):

```powershell
kubectl --context docker-desktop --namespace sportapp-local port-forward service/bff-web 8000:80
```

Terminal 2, servicio de usuarios (opcional, para ver su API interna):

```powershell
kubectl --context docker-desktop --namespace sportapp-local port-forward service/users-management 8001:80
```

**Qué debe ver en cada una:** `Forwarding from 127.0.0.1:8000 -> 8000` (o `8001`). La terminal queda "ocupada": es lo esperado.

| Si ve | Causa | Solución |
| --- | --- | --- |
| `unable to listen on port 8000: ... address already in use` | Otro programa usa el puerto | Use otro puerto local, por ejemplo `8010:80`, y abra `http://127.0.0.1:8010/docs`. |
| `error: ... pod is not running` | El pod aún no está listo | Paso 11.5. |

#### 12.2 Abrir la documentación OpenAPI (Swagger)

| Componente | Documentación interactiva | JSON del contrato | Salud |
| --- | --- | --- | --- |
| BFF web | http://127.0.0.1:8000/docs | http://127.0.0.1:8000/openapi.json | http://127.0.0.1:8000/health/ready |
| users-management | http://127.0.0.1:8001/docs | http://127.0.0.1:8001/openapi.json | http://127.0.0.1:8001/health/ready |

En `/health/ready` debe ver `{"status":"ok","checks":{...}}`.

Smoke test desde otra terminal:

```powershell
uv run python scripts/task.py smoke --base-url http://127.0.0.1:8000
```

**Qué debe ver:** `OK /health/live` y `OK /health/ready`.

#### 12.3 Probar el registro de usuario (HU001) desde Swagger

1. Abra http://127.0.0.1:8000/docs.
2. Despliegue **POST /v1/users** y pulse **Try it out**.
3. En **Idempotency-Key** pegue un UUID nuevo. Para generarlo, en PowerShell: `[guid]::NewGuid().ToString()`.
4. En **Request body** pegue:

```json
{
  "full_name": "Ana Pérez",
  "email": "ana@example.com",
  "password": "una-clave-de-prueba",
  "actor_type": "athlete",
  "accepted_policies_version": "local-dev"
}
```

5. Pulse **Execute**.

**Qué debe ver:** código **201** y un cuerpo con `user_id`, `status: "active"` y `aggregate_version: 1`.

Pruebe también los demás criterios de aceptación:

| Prueba | Cómo | Resultado esperado |
| --- | --- | --- |
| Reintento idempotente (CA5) | **Execute** otra vez con la misma clave y el mismo cuerpo | **201** con el mismo `user_id` y la cabecera `idempotent-replayed: true` |
| Correo duplicado (CA2) | Clave nueva, mismo correo (aunque cambien mayúsculas) | **409** `email_already_registered` |
| Datos inválidos (CA3) | Clave nueva y `"password": "corta"` | **422** `validation_error`, con el campo indicado |
| Políticas no vigentes | Clave nueva y `"accepted_policies_version": "2020-01"` | **422** `policies_version_mismatch` |

> `accepted_policies_version` debe ser `local-dev`, que es el valor configurado para el entorno local. La versión real de las políticas aún no está publicada.

#### 12.4 Ver que el evento se publicó (CA4)

```powershell
kubectl --context docker-desktop --namespace sportapp-local logs deployment/users-management-outbox-publisher --tail=20
```

**Qué debe ver:** una línea JSON con `"message": "outbox.published"` y `"event_type": "usuario.registrado.v1"` por cada registro creado.

### 13. Recolectar diagnóstico para pedir ayuda

Si algo no funciona, ejecute este bloque. Guarda un archivo `diagnostico-k8s.txt` en la carpeta `backend`. **No incluye contraseñas**: solo versiones, estados y logs.

Ejecute las líneas **una por una**:

```powershell
$f = ".\diagnostico-k8s.txt"
```

```powershell
"== versiones ==" | Out-File $f -Encoding utf8; docker version --format "docker {{.Server.Version}}" | Out-File $f -Append -Encoding utf8; uv --version | Out-File $f -Append -Encoding utf8
```

```powershell
"== contextos ==" | Out-File $f -Append -Encoding utf8; kubectl config get-contexts | Out-File $f -Append -Encoding utf8
```

```powershell
"== nodos ==" | Out-File $f -Append -Encoding utf8; kubectl --context docker-desktop get nodes -o wide 2>&1 | Out-File $f -Append -Encoding utf8
```

```powershell
"== imagenes ==" | Out-File $f -Append -Encoding utf8; docker images --filter "reference=sportapp/*" | Out-File $f -Append -Encoding utf8
```

```powershell
"== recursos ==" | Out-File $f -Append -Encoding utf8; kubectl --context docker-desktop --namespace sportapp-local get pods,jobs,deployments,statefulsets,pvc -o wide 2>&1 | Out-File $f -Append -Encoding utf8
```

```powershell
"== eventos ==" | Out-File $f -Append -Encoding utf8; kubectl --context docker-desktop --namespace sportapp-local get events --sort-by=.lastTimestamp 2>&1 | Select-Object -Last 60 | Out-File $f -Append -Encoding utf8
```

```powershell
"== logs postgres ==" | Out-File $f -Append -Encoding utf8; kubectl --context docker-desktop --namespace sportapp-local logs statefulset/postgres --tail=60 2>&1 | Out-File $f -Append -Encoding utf8
```

```powershell
"== logs migracion usuarios ==" | Out-File $f -Append -Encoding utf8; kubectl --context docker-desktop --namespace sportapp-local logs job/users-management-migrate-write --tail=60 2>&1 | Out-File $f -Append -Encoding utf8
```

Abra el archivo con `notepad .\diagnostico-k8s.txt` y comparta su contenido.

> `diagnostico-k8s.txt` está ignorado por Git. Bórrelo cuando ya no lo necesite: `Remove-Item .\diagnostico-k8s.txt`.

### 14. Detener, reiniciar y limpiar

Todos los comandos actúan **solo** sobre el clúster local (`--context docker-desktop`) y el namespace de SportApp.

#### 14.1 Detener SportApp (conserva los datos)

```powershell
kubectl --context docker-desktop --namespace sportapp-local delete --kustomize deploy/k8s/overlays/local
```

Elimina pods, servicios y Jobs, pero **conserva** el volumen de PostgreSQL. Para volver a levantar, repita el paso 11.

#### 14.2 Desplegar un cambio de código

```powershell
uv run python scripts/task.py build users-management
```

```powershell
kubectl --context docker-desktop --namespace sportapp-local rollout restart deployment/users-management
```

Reemplace `users-management` por el componente que cambió.

#### 14.3 Repetir las migraciones

Necesario cuando se agrega una migración nueva o un Job quedó fallido. Los Jobs no se pueden modificar, así que primero se eliminan y luego se aplica de nuevo:

```powershell
kubectl --context docker-desktop --namespace sportapp-local delete job --selector app.kubernetes.io/component=migration
```

```powershell
kubectl --context docker-desktop --namespace sportapp-local apply --kustomize deploy/k8s/overlays/local
```

#### 14.4 Empezar de cero (borra los datos locales)

Úselo si cambió las contraseñas del paso 9 o si la base quedó en mal estado. **Borra todos los datos de PostgreSQL local.**

```powershell
kubectl --context docker-desktop --namespace sportapp-local delete --kustomize deploy/k8s/overlays/local
```

```powershell
kubectl --context docker-desktop --namespace sportapp-local delete pvc --selector app.kubernetes.io/name=postgres
```

Luego vuelva al paso 11.1.

#### 14.5 Después de reiniciar Windows o Docker Desktop

1. Abra Docker Desktop y espere a que Kubernetes quede en verde.
2. Los recursos vuelven solos. Revise con `kubectl --context docker-desktop --namespace sportapp-local get pods`.
3. Abra de nuevo los `port-forward` (paso 12.1): no sobreviven al reinicio.

#### 14.6 Comandos que NO debe usar para limpiar

| Comando | Por qué no |
| --- | --- |
| `kubectl delete namespace ...` sin `--context` | Podría ejecutarse sobre un clúster GKE si es el contexto activo. |
| `kubectl delete ... --all` | Borra más de lo necesario. |
| *Reset Kubernetes cluster* en Docker Desktop | Elimina el clúster completo. Úselo solo como último recurso. |

### 15. Glosario

| Término | Significado |
| --- | --- |
| **Docker Desktop** | Aplicación que ejecuta contenedores en Windows y puede crear un clúster Kubernetes local. |
| **WSL 2** | Subsistema de Linux de Windows, sobre el que funciona Docker Desktop. |
| **Imagen** | Paquete inmutable con la aplicación y sus dependencias, por ejemplo `sportapp/users-management:local`. |
| **Contenedor** | Una imagen en ejecución. |
| **Kubernetes** | Orquestador que arranca contenedores, los reinicia si fallan y los conecta entre sí. |
| **Clúster** | Conjunto de máquinas (aquí una sola, `docker-desktop`) administradas por Kubernetes. |
| **Contexto** | Clúster al que apunta `kubectl`. Se elige con `--context docker-desktop`. |
| **Namespace** | Espacio aislado dentro del clúster. SportApp usa `sportapp-local`. |
| **Pod** | Unidad mínima de ejecución; normalmente un contenedor. |
| **Deployment** | Mantiene corriendo un número de pods de un componente, por ejemplo `users-management`. |
| **StatefulSet** | Como un Deployment, pero con almacenamiento estable. Se usa para `postgres`. |
| **Job** | Tarea que se ejecuta hasta terminar, como las migraciones o la creación del tópico. |
| **Service** | Nombre de red estable para llegar a los pods, por ejemplo `http://users-management`. |
| **PVC / volumen** | Disco persistente; guarda los datos de PostgreSQL entre reinicios. |
| **port-forward** | Túnel temporal desde `127.0.0.1:<puerto>` de su equipo hacia un Service del clúster. |
| **Kustomize** | Forma de componer manifiestos: `deploy/k8s/base` más los ajustes de `deploy/k8s/overlays/local`. |
| **uv** | Gestor de Python y dependencias del proyecto (`uv sync`, `uv run`). |
| **Emulador de Pub/Sub** | Versión local del servicio de mensajería de Google Cloud. |

### Estado de verificación de esta guía

| Parte | Estado |
| --- | --- |
| Pasos 2 a 6, 9 y 10 | Verificados en el equipo de desarrollo el 2026-10-07. |
| Paso 6.2 (`ci-fast`) | Ejecutado de nuevo el 2026-10-08: código de salida 0 en 37 s, 117 pruebas unitarias en 9 paquetes. |
| Paso 7 y 8.2 | Verificados: clúster `docker-desktop` creado con kubeadm, nodo `Ready`, Kubernetes v1.36.1, storageclass `hostpath`. |
| Manifiestos del paso 11 | Validados sin aplicarlos: 34 recursos válidos contra el esquema de Kubernetes 1.33 (kubeconform estricto). |
| Pasos 11 a 14 en el clúster | **Pendiente de la primera ejecución.** Si un resultado difiere de lo descrito, comparta el diagnóstico de la sección 13 para corregir la guía. |

## Integración continua

`.github/workflows/ci.yml`:

| Evento | Checks |
| --- | --- |
| push a `feature/**`, `fix/**`, `chore/**`, `release/**`, `hotfix/**` | `quality`, `unit`, `secrets` |
| pull request a `develop` o `main` | los anteriores más `integration-coverage` (PostgreSQL real, emulador de Pub/Sub, suites de integración, contrato y componente, umbral 80 %), `security` e `image (<componente>)` |

`.github/workflows/deliver-development.yml` (merge a `develop`) está **bloqueado** hasta ratificar GCP y Workload Identity Federation. Sin las variables del Environment `development` informa el bloqueo y no publica nada. El job `deploy` falla de forma explícita hasta definir el destino.

Las acciones están fijadas a SHA completo, con permisos mínimos y sin llaves JSON. Ningún workflow fusiona ramas.

## Documentación relacionada

- La arquitectura, las decisiones y los cursos de formación (uv, Terraform, GitHub Actions) están en la documentación central del proyecto (`documentacion/`), fuera de este repositorio.
- `scripts/task.py` también está documentado en la documentación central, en `integracion-continua/02-matriz-pipelines-por-repositorio.md`, subsección «Comandos del repositorio backend».

## Pendiente de decisión (no implementado)

- Ratificar ADR-019. Configurar los rulesets de `develop`/`main`, `CODEOWNERS` y aprobadores. Hoy solo existe la rama `master`.
- GCP: proyectos, Workload Identity Provider, Artifact Registry, destino GKE/Cloud Run y Terraform (ADR-017).
- Manifiesto de release, workflows de staging/producción, SBOM y escaneo de imágenes.
- Contrato compartido de errores, sobre de eventos y primeros esquemas. Tópicos, suscripciones y DLQ.
- Proyector de lectura de usuarios (`usuario_read`, suscripción `proyector-usuarios-v1`, DLQ) y los workers CQRS de entrenamientos y eventos (CMP-002).
- HU001:
  - prueba de carga HA003 en un ambiente representativo (escenario listo en `load/locust/registration.py`);
  - protección contra abuso y enumeración en el registro (el `409 email_already_registered` revela qué correos existen);
  - versión real de las políticas;
  - purga de claves de idempotencia vencidas;
  - `T-HU001-PLAT`: tópico `sportapp.afiliacion.v1`, suscripción del alta gratuita con orden, DLQ, política de reintentos, IAM, métricas y alerta en la composición local y en GCP (en este repositorio solo existe la creación en el emulador);
  - medir HA002 en el recorrido registro → suscripción `FREE` observable (presupuesto de 3 s bajo la carga acordada);
  - `PROVISIONING` y la evidencia del resultado `FREE` en el cliente web, que debe consumir `GET /v1/me/capabilities` del BFF (ya disponible).
- HU003:
  - procedimiento de rotación de la clave de firma (el JWKS ya admite varias claves; falta publicar la anterior durante la transición);
  - purga de refresh tokens vencidos y de ventanas de intentos antiguas;
  - IP real del usuario detrás del balanceador: Uvicorn solo confía en `X-Forwarded-For` de las IP en `FORWARDED_ALLOW_IPS` (por defecto `127.0.0.1`), que debe fijar la plataforma;
  - revocación por incidente y cierre de sesión (HU004), MFA y recuperación de cuenta;
  - clientes web y móvil (almacenamiento seguro y E2E).
- Ubicación ratificada de Playwright para API/E2E. Las pruebas de componente BFF → servicio ya existen para HU001 (`bff/web/tests/component`).
