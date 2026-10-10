# Esquemas de eventos

Cada esquema se ubica en `<dominio>/<tipo>.vN.schema.json` y debe incluir el sobre mínimo de los lineamientos: `event_id`, `event_type`, `occurred_at`, `producer`, `aggregate_id`, `aggregate_version`, `correlation_id`, `causation_id`, `schema_version` y `payload`.

| Evento | Tópico | Ordering key | Productor | Estado |
| --- | --- | --- | --- | --- |
| [`usuario.registrado.v1`](usuarios/usuario.registrado.v1.schema.json) | `sportapp.usuarios.v1` | `aggregate_id` (usuario) | `users_management` (`usuarios-outbox-publisher`) | Aprobado 2026-10-07 (HU001) |

Los atributos del mensaje Pub/Sub repiten `event_id`, `event_type`, `schema_version` y `correlation_id` para filtrar sin leer el cuerpo.
