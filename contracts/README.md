# Contratos versionados del backend

Los contratos son la fuente compartida entre backend, BFF, Angular y Android. No se escriben después de implementar: se aprueban antes y se validan en CI.

| Ruta | Contenido | Validación |
| --- | --- | --- |
| `openapi/<componente>.openapi.json` | OpenAPI generado por cada microservicio y BFF. `info.version` es la versión del contrato. | `tests/contract/test_openapi.py` de cada componente compara el código contra este archivo. |
| `events/<dominio>/<tipo>.vN.schema.json` | JSON Schema de cada evento aprobado (por ejemplo `usuario.registrado.v1`). | `libs/sportapp_platform/tests/contract/test_event_schemas.py` exige JSON Schema válido y nombre versionado. |
| `authorization/actor-permissions.vN.json` | Permisos estables por actor y canal (ADR-025). La imagen OCI lo copia y `ACTOR_PERMISSIONS_PATH` lo indica. | `libs/sportapp_platform/tests/unit/test_authorization.py` exige exactamente los códigos, HU y canales de ADR-025; el arranque falla si falta o es inválido. |

## Estado

- Todos los componentes publican `/health/live` y `/health/ready`.
- `users-management`: OpenAPI `0.5.0` con `POST /v1/users` (HU001, aprobado el 2026-10-07), `POST /v1/sessions` y `POST /v1/sessions/refresh` (HU003, aprobado el 2026-10-10), `POST /v1/sessions/revoke` (HU004, aprobado el 2026-10-12: siempre `204`, revoca la familia del refresh token, `Idempotency-Key` opcional sin persistir por ADR-026), `GET /.well-known/jwks.json` y el contrato **interno** `POST /v1/users/lookup` (HU010, aprobado el 2026-10-12: correo exacto → `user_id` y `actor_type`, `404 user_not_found`; lee la base de lectura y solo lo consume `bff-web`).
- `bff-web`: OpenAPI `0.5.0` (2026-10-11). La 0.4.0 fue un **cambio incompatible pre-1.0**, aprobado sin versión coexistente porque ningún cliente publicado usaba la 0.3.0: el refresh token pasa del cuerpo a la cookie `HttpOnly` `__Secure-sportapp_refresh`, `POST /v1/sessions/refresh` va sin cuerpo y ambas operaciones exigen un `Origin` permitido (`403 origin_not_allowed`). La 0.5.0 agrega, sin romper nada, `GET /v1/me/capabilities` (HU003 CA7-CA10). La 0.6.0 (2026-10-12) agrega, sin romper nada, HU010: `POST /v1/me/training-authorizations`, `DELETE /v1/me/training-authorizations/{professional_id}`, `POST /v1/training-plans` y `GET /v1/training-plans/{plan_id}`; CORS admite además `DELETE`. La 0.8.0 (2026-10-12) agrega, sin romper nada, el historial de entrenamiento de HU011 (`GET /v1/training-history` y `GET /v1/athletes/{athlete_id}/training-history`). La 0.7.0 (2026-10-12) agrega, sin romper nada, `POST /v1/sessions/revoke` (HU004): sin cuerpo, con la cookie y `Origin` permitido; responde `204` y borra la cookie, también con `503`/`504`.
- `bff-mobile`: OpenAPI `0.4.0` con las operaciones de sesión de HU003, los tokens en el cuerpo, `GET /v1/me/capabilities` con el mismo esquema que `bff-web` y, desde 0.4.0 (2026-10-12, sin romper nada), `POST /v1/sessions/revoke` con `{refresh_token}` e `Idempotency-Key` opcional que la cola offline repite en cada reintento (HU004, ADR-026).
- `authorization/actor-permissions.v1.json` (ADR-025): permisos de `organizer` y `service_provider` por canal; `athlete` no tiene permisos de rol (sus capacidades vienen del plan).
- `affiliation-management`: OpenAPI `0.2.0` con el contrato **interno** `GET /v1/subscriptions/by-user/{user_id}/capabilities` (HU001, ADR-024); solo lo consumen los BFF.
- `training-management`: OpenAPI `0.3.0` (HU011 agrega, sin romper nada, `GET /v1/training-history` y `GET /v1/athletes/{athlete_id}/training-history`). Desde 0.2.0 (HU010, aprobado el 2026-10-12): `PUT`/`DELETE /v1/training-authorizations/{professional_id}`, `POST /v1/training-plans` y `GET /v1/training-plans/{plan_id}`. Interno: lo consume `bff-web` reenviando el access token del usuario, que el servicio vuelve a verificar. El resto de los componentes sigue en `0.1.0`.
- Eventos aprobados: `usuario.registrado.v1`, `suscripcion.activada.v1`, `plan.entrenamiento_creado.v1`, `autorizacion_entrenamiento.otorgada.v1`, `autorizacion_entrenamiento.revocada.v1` y `entrenamiento.finalizado.v1` (ver `events/README.md`). Los eventos candidatos de las guías de dominio no son contratos: se agregan aquí únicamente cuando tienen aprobados la versión, el payload mínimo, el tópico, la ordering key, los reintentos y la DLQ.
- Formato de error: implementa la propuesta `code`, `message`, `correlation_id`, `details[{field, issue}]` de los lineamientos transversales. Permanece **propuesto** hasta publicar el contrato compartido.

## Cambiar un contrato

1. Acordar la operación con los consumidores (BFF, web o móvil).
2. Implementar el cambio y regenerar: `uv run python scripts/task.py openapi-export`.
3. Si es incompatible, publicar una nueva versión coexistente; nunca reemplazar en silencio.
4. Revisar el diff del contrato en el pull request junto con su versión.
