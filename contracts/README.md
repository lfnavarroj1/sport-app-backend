# Contratos versionados del backend

Los contratos son la fuente compartida entre backend, BFF, Angular y Android. No se escriben después de implementar: se aprueban antes y se validan en CI.

| Ruta | Contenido | Validación |
| --- | --- | --- |
| `openapi/<componente>.openapi.json` | OpenAPI generado por cada microservicio y BFF. `info.version` es la versión del contrato. | `tests/contract/test_openapi.py` de cada componente compara el código contra este archivo. |
| `events/<dominio>/<tipo>.vN.schema.json` | JSON Schema de cada evento aprobado (por ejemplo `usuario.registrado.v1`). | `libs/sportapp_platform/tests/contract/test_event_schemas.py` exige JSON Schema válido y nombre versionado. |

## Estado

- Todos los componentes publican `/health/live` y `/health/ready`.
- `users-management` y `bff-web`: OpenAPI `0.2.0` con `POST /v1/users` (HU001, aprobado el 2026-10-07). El resto de los componentes sigue en `0.1.0`.
- Eventos aprobados: `usuario.registrado.v1` (ver `events/README.md`). Los eventos candidatos de las guías de dominio no son contratos: se agregan aquí únicamente cuando tienen aprobados la versión, el payload mínimo, el tópico, la ordering key, los reintentos y la DLQ.
- Formato de error: implementa la propuesta `code`, `message`, `correlation_id`, `details[{field, issue}]` de los lineamientos transversales. Permanece **propuesto** hasta publicar el contrato compartido.

## Cambiar un contrato

1. Acordar la operación con los consumidores (BFF, web o móvil).
2. Implementar el cambio y regenerar: `uv run python scripts/task.py openapi-export`.
3. Si es incompatible, publicar una nueva versión coexistente; nunca reemplazar en silencio.
4. Revisar el diff del contrato en el pull request junto con su versión.
