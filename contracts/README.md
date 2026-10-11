# Contratos versionados del backend

Los contratos son la fuente compartida entre backend, BFF, Angular y Android. No se escriben después de implementar: se aprueban antes y se validan en CI.

| Ruta | Contenido | Validación |
| --- | --- | --- |
| `openapi/<componente>.openapi.json` | OpenAPI generado por cada microservicio y BFF. `info.version` es la versión del contrato. | `tests/contract/test_openapi.py` de cada componente compara el código contra este archivo. |
| `events/<dominio>/<tipo>.vN.schema.json` | JSON Schema de cada evento aprobado (por ejemplo `usuario.registrado.v1`). | `libs/sportapp_platform/tests/contract/test_event_schemas.py` exige JSON Schema válido y nombre versionado. |
| `authorization/actor-permissions.vN.json` | Permisos estables por actor y canal (ADR-025). La imagen OCI lo copia y `ACTOR_PERMISSIONS_PATH` lo indica. | `libs/sportapp_platform/tests/unit/test_authorization.py` exige exactamente los códigos, HU y canales de ADR-025; el arranque falla si falta o es inválido. |

## Estado

- Todos los componentes publican `/health/live` y `/health/ready`.
- `users-management`: OpenAPI `0.3.0` con `POST /v1/users` (HU001, aprobado el 2026-10-07), `POST /v1/sessions` y `POST /v1/sessions/refresh` (HU003, aprobado el 2026-10-10) y `GET /.well-known/jwks.json`.
- `bff-web`: OpenAPI `0.5.0` (2026-10-11). La 0.4.0 fue un **cambio incompatible pre-1.0**, aprobado sin versión coexistente porque ningún cliente publicado usaba la 0.3.0: el refresh token pasa del cuerpo a la cookie `HttpOnly` `__Secure-sportapp_refresh`, `POST /v1/sessions/refresh` va sin cuerpo y ambas operaciones exigen un `Origin` permitido (`403 origin_not_allowed`). La 0.5.0 agrega, sin romper nada, `GET /v1/me/capabilities` (HU003 CA7-CA10).
- `bff-mobile`: OpenAPI `0.3.0` con las operaciones de sesión de HU003, los tokens en el cuerpo y `GET /v1/me/capabilities` con el mismo esquema que `bff-web`.
- `authorization/actor-permissions.v1.json` (ADR-025): permisos de `organizer` y `service_provider` por canal; `athlete` no tiene permisos de rol (sus capacidades vienen del plan).
- `affiliation-management`: OpenAPI `0.2.0` con el contrato **interno** `GET /v1/subscriptions/by-user/{user_id}/capabilities` (HU001, ADR-024); solo lo consumen los BFF. El resto de los componentes sigue en `0.1.0`.
- Eventos aprobados: `usuario.registrado.v1` y `suscripcion.activada.v1` (ver `events/README.md`). Los eventos candidatos de las guías de dominio no son contratos: se agregan aquí únicamente cuando tienen aprobados la versión, el payload mínimo, el tópico, la ordering key, los reintentos y la DLQ.
- Formato de error: implementa la propuesta `code`, `message`, `correlation_id`, `details[{field, issue}]` de los lineamientos transversales. Permanece **propuesto** hasta publicar el contrato compartido.

## Cambiar un contrato

1. Acordar la operación con los consumidores (BFF, web o móvil).
2. Implementar el cambio y regenerar: `uv run python scripts/task.py openapi-export`.
3. Si es incompatible, publicar una nueva versión coexistente; nunca reemplazar en silencio.
4. Revisar el diff del contrato en el pull request junto con su versión.
