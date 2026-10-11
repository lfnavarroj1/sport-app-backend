# Esquemas de eventos

Cada esquema se ubica en `<dominio>/<tipo>.vN.schema.json` y debe incluir el sobre mínimo de los lineamientos: `event_id`, `event_type`, `occurred_at`, `producer`, `aggregate_id`, `aggregate_version`, `correlation_id`, `causation_id`, `schema_version` y `payload`.

| Evento | Tópico | Ordering key | Productor | Estado |
| --- | --- | --- | --- | --- |
| [`usuario.registrado.v1`](usuarios/usuario.registrado.v1.schema.json) | `sportapp.usuarios.v1` | `aggregate_id` (usuario) | `users_management` (`usuarios-outbox-publisher`) | Aprobado 2026-10-07 (HU001) |
| [`suscripcion.activada.v1`](afiliacion/suscripcion.activada.v1.schema.json) | `sportapp.afiliacion.v1` | `aggregate_id` (= `user_id`) | `affiliation_management` (`afiliacion-outbox-publisher`) | Aprobado 2026-10-11 (ADR-024, HU001) |

Consumidores aprobados:

| Evento consumido | Consumidor | Suscripción | DLQ | Reintentos |
| --- | --- | --- | --- | --- |
| `usuario.registrado.v1` | `affiliation_management` (alta gratuita) | `sportapp.afiliacion.alta-gratuita.v1` (con orden) | `sportapp.afiliacion.alta-gratuita.dlq` (+ `…dlq.ops`) | 10 intentos; backoff de 10 a 600 s; ACK después del COMMIT |

`suscripcion.activada.v1` todavía no tiene consumidores aprobados: no se crea suscripción ni DLQ para él (ADR-024).

Los atributos del mensaje Pub/Sub repiten `event_id`, `event_type`, `schema_version` y `correlation_id` para filtrar sin leer el cuerpo.
