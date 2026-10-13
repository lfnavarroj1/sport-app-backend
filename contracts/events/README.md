# Esquemas de eventos

Cada esquema se ubica en `<dominio>/<tipo>.vN.schema.json` y debe incluir el sobre mínimo de los lineamientos: `event_id`, `event_type`, `occurred_at`, `producer`, `aggregate_id`, `aggregate_version`, `correlation_id`, `causation_id`, `schema_version` y `payload`.

| Evento | Tópico | Ordering key | Productor | Estado |
| --- | --- | --- | --- | --- |
| [`usuario.registrado.v1`](usuarios/usuario.registrado.v1.schema.json) | `sportapp.usuarios.v1` | `aggregate_id` (usuario) | `users_management` (`usuarios-outbox-publisher`) | Aprobado 2026-10-07 (HU001) |
| [`suscripcion.activada.v1`](afiliacion/suscripcion.activada.v1.schema.json) | `sportapp.afiliacion.v1` | `aggregate_id` (= `user_id`) | `affiliation_management` (`afiliacion-outbox-publisher`) | Aprobado 2026-10-11 (ADR-024, HU001) |
| [`plan.entrenamiento_creado.v1`](entrenamientos/plan.entrenamiento_creado.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (= `plan_id`) | `training_management` (`entrenamientos-outbox-publisher`) | Aprobado 2026-10-12 (CMP-003, HU010) |
| [`autorizacion_entrenamiento.otorgada.v1`](entrenamientos/autorizacion_entrenamiento.otorgada.v1.schema.json) y [`autorizacion_entrenamiento.revocada.v1`](entrenamientos/autorizacion_entrenamiento.revocada.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (UUIDv5 de `athlete_id:professional_id`) | `training_management` (`entrenamientos-outbox-publisher`) | Aprobados 2026-10-12 (HU011); estado completo de la autorización |
| [`entrenamiento.finalizado.v1`](entrenamientos/entrenamiento.finalizado.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (= `session_id`) | `training_management` (`entrenamientos-outbox-publisher`, desde HU032; versión 2 del agregado) | Aprobado 2026-10-12 (CMP-003, HU011); esquema sin cambios en HU032 |
| [`entrenamiento.iniciado.v1`](entrenamientos/entrenamiento.iniciado.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (= `session_id`) | `training_management` (`entrenamientos-outbox-publisher`; versión 1 del agregado) | Aprobado 2026-10-12 (ADR-029, HU032) |
| [`evento.creado.v1`](eventos/evento.creado.v1.schema.json) | `sportapp.eventos.v1` | `aggregate_id` (= `event_id`) | `events_management` (`eventos-outbox-publisher`) | Aprobado 2026-10-12 (ADR-028, CMP-004, HU021); estado completo del borrador, instantes en UTC (`Z`) |
| [`usuario.inscrito.v1`](eventos/usuario.inscrito.v1.schema.json) | `sportapp.eventos.v1` | `aggregate_id` (= `registration_id`) | `events_management` (`eventos-outbox-publisher`; versión 1 del agregado `event_registration`) | Aprobado 2026-10-12 (ADR-032, HU041); sin datos del evento ni de pago; `applied_fee` informativa o `null` |

Consumidores aprobados:

| Evento consumido | Consumidor | Suscripción | DLQ | Reintentos |
| --- | --- | --- | --- | --- |
| `usuario.registrado.v1` | `affiliation_management` (alta gratuita) | `sportapp.afiliacion.alta-gratuita.v1` (con orden) | `sportapp.afiliacion.alta-gratuita.dlq` (+ `…dlq.ops`) | 10 intentos; backoff de 10 a 600 s; ACK después del COMMIT |
| eventos de `sportapp.entrenamientos.v1` (hoy proyecta `plan.entrenamiento_creado.v1`, `autorizacion_entrenamiento.*.v1`, `entrenamiento.iniciado.v1` y `entrenamiento.finalizado.v1`; otros tipos se confirman sin efectos) | `training_management` (`entrenamientos-read-projector`) | `proyector-entrenamientos-v1` (con orden) | `sportapp.entrenamientos.proyeccion.dlq` (+ `…dlq.ops`) | 10 intentos; backoff de 10 a 600 s; ACK después del COMMIT (PROPUESTO en HU010; adoptado por la tarea de HU032 bajo ADR-029) |
| eventos de `sportapp.eventos.v1` (hoy proyecta `evento.creado.v1` en `event_read` y `usuario.inscrito.v1` en `event_registration_read`; otros tipos se confirman sin efectos) | `events_management` (`eventos-read-projector`) | `proyector-eventos-v1` (con orden) | `sportapp.eventos.proyeccion.dlq` (+ `…dlq.ops`) | Aprobado en ADR-028: 10 intentos; backoff de 10 a 600 s; deduplicación por `event_id` del sobre; ACK después del COMMIT |

`suscripcion.activada.v1` todavía no tiene consumidores aprobados: no se crea suscripción ni DLQ para él (ADR-024).

Los atributos del mensaje Pub/Sub repiten `event_id`, `event_type`, `schema_version` y `correlation_id` para filtrar sin leer el cuerpo.
