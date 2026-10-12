# Esquemas de eventos

Cada esquema se ubica en `<dominio>/<tipo>.vN.schema.json` y debe incluir el sobre mínimo de los lineamientos: `event_id`, `event_type`, `occurred_at`, `producer`, `aggregate_id`, `aggregate_version`, `correlation_id`, `causation_id`, `schema_version` y `payload`.

| Evento | Tópico | Ordering key | Productor | Estado |
| --- | --- | --- | --- | --- |
| [`usuario.registrado.v1`](usuarios/usuario.registrado.v1.schema.json) | `sportapp.usuarios.v1` | `aggregate_id` (usuario) | `users_management` (`usuarios-outbox-publisher`) | Aprobado 2026-10-07 (HU001) |
| [`suscripcion.activada.v1`](afiliacion/suscripcion.activada.v1.schema.json) | `sportapp.afiliacion.v1` | `aggregate_id` (= `user_id`) | `affiliation_management` (`afiliacion-outbox-publisher`) | Aprobado 2026-10-11 (ADR-024, HU001) |
| [`plan.entrenamiento_creado.v1`](entrenamientos/plan.entrenamiento_creado.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (= `plan_id`) | `training_management` (`entrenamientos-outbox-publisher`) | Aprobado 2026-10-12 (CMP-003, HU010) |
| [`autorizacion_entrenamiento.otorgada.v1`](entrenamientos/autorizacion_entrenamiento.otorgada.v1.schema.json) y [`autorizacion_entrenamiento.revocada.v1`](entrenamientos/autorizacion_entrenamiento.revocada.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (UUIDv5 de `athlete_id:professional_id`) | `training_management` (`entrenamientos-outbox-publisher`) | Aprobados 2026-10-12 (HU011); estado completo de la autorización |
| [`entrenamiento.finalizado.v1`](entrenamientos/entrenamiento.finalizado.v1.schema.json) | `sportapp.entrenamientos.v1` | `aggregate_id` (= `session_id`) | `training_management` (lo publicará HU032; sin productor todavía) | Aprobado 2026-10-12 (CMP-003, HU011) |

Consumidores aprobados:

| Evento consumido | Consumidor | Suscripción | DLQ | Reintentos |
| --- | --- | --- | --- | --- |
| `usuario.registrado.v1` | `affiliation_management` (alta gratuita) | `sportapp.afiliacion.alta-gratuita.v1` (con orden) | `sportapp.afiliacion.alta-gratuita.dlq` (+ `…dlq.ops`) | 10 intentos; backoff de 10 a 600 s; ACK después del COMMIT |
| eventos de `sportapp.entrenamientos.v1` (hoy proyecta `plan.entrenamiento_creado.v1`, `autorizacion_entrenamiento.*.v1` y `entrenamiento.finalizado.v1`; otros tipos se confirman sin efectos) | `training_management` (`entrenamientos-read-projector`) | `proyector-entrenamientos-v1` (con orden) | `sportapp.entrenamientos.proyeccion.dlq` (+ `…dlq.ops`) | PROPUESTO: 10 intentos; backoff de 10 a 600 s; ACK después del COMMIT |

`suscripcion.activada.v1` todavía no tiene consumidores aprobados: no se crea suscripción ni DLQ para él (ADR-024).

Los atributos del mensaje Pub/Sub repiten `event_id`, `event_type`, `schema_version` y `correlation_id` para filtrar sin leer el cuerpo.
