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
- [Integración continua, release y despliegue](#integración-continua-release-y-despliegue)
- [Documentación relacionada](#documentación-relacionada)
- [Pendiente de decisión (no implementado)](#pendiente-de-decisión-no-implementado)

## Estado

Todos los componentes exponen `/health/live` y `/health/ready`. Historias implementadas en backend:

| HU | Operación | Componentes | Contrato |
| --- | --- | --- | --- |
| HU001 Registrar usuario | `POST /v1/users` (público, `Idempotency-Key` obligatorio); interno: `GET /v1/subscriptions/by-user/{user_id}/capabilities` en Afiliación | `bff_web` → `users_management` → `user_account` + `outbox_event` → `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → consumidor del alta gratuita de `affiliation_management` → `subscription` `FREE` + `outbox_event` → `afiliacion-outbox-publisher` → `sportapp.afiliacion.v1` | OpenAPI de `users-management`, `bff-web` y `affiliation-management` (0.2.0); eventos `usuario.registrado.v1` y `suscripcion.activada.v1` |
| HU003 Iniciar sesión | `POST /v1/sessions` y `POST /v1/sessions/refresh` (públicos); `GET /v1/me/capabilities` (con access token) en ambos BFF; `GET /.well-known/jwks.json` solo en el servicio | `bff_web` y `bff_mobile` → `users_management` → `refresh_token` + `login_failure_window`; para capacidades, BFF → JWKS de Usuarios + `actor-permissions.v1` + Afiliación (solo `athlete`) | OpenAPI 0.3.0 de `users-management`, 0.5.0 de `bff-web` (refresh en cookie `HttpOnly`) y 0.3.0 de `bff-mobile`; contrato `contracts/authorization/actor-permissions.v1.json`; sin eventos |
| HU004 Cerrar sesión | `POST /v1/sessions/revoke` en el servicio y en ambos BFF (web: sin cuerpo, con la cookie y `Origin` permitido; móvil: `{refresh_token}`) | `bff_web` y `bff_mobile` → `users_management` → revoca la familia en `refresh_token` | OpenAPI 0.5.0 de `users-management`, 0.7.0 de `bff-web` y 0.4.0 de `bff-mobile`; sin eventos |
| HU005 Registrar datos demográficos y fisiológicos | `PUT /v1/me/demographic-profile`, `PUT /v1/me/physiological-profile` (`Idempotency-Key` obligatorio, `expected_version` al reemplazar) y `GET /v1/me/profile`, solo `athlete` | `bff_web` → `users_management` → `demographic_profile` / `physiological_profile` + `idempotency_key` + `outbox_event` → `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → `usuarios-read-projector` → `athlete_profile_view` (y `user_directory` desde `usuario.registrado.v1`) + `processed_event` | OpenAPI 0.6.0 de `users-management` y 0.13.0 de `bff-web`; eventos `perfil.demografico_actualizado.v1` y `perfil.fisiologico_actualizado.v1`; ADR-034 |
| HU006 Registrar hábitos deportivos | `PUT /v1/me/sport-habits` (`Idempotency-Key` obligatorio, `expected_version` al reemplazar), solo `athlete`; la consulta es `GET /v1/me/profile` (HU005) | `bff_web` → `users_management` → `sport_habits` + `idempotency_key` + `outbox_event` → `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → `usuarios-read-projector` → `athlete_profile_view.sport_habits` + `processed_event` | OpenAPI 0.9.0 de `users-management` y 0.19.0 de `bff-web`; evento `habitos.deportivos_actualizados.v1`; ADR-034 |
| HU009 Registrar hábitos alimenticios | `PUT /v1/me/nutrition-habits` (`Idempotency-Key` obligatorio, `expected_version` al reemplazar, consentimiento `HEALTH_DATA_CONSENT_VERSION`), solo `athlete`; la consulta es `GET /v1/me/profile` (HU005); interno: `GET /v1/users/{user_id}/nutrition-restrictions` (solo Entrenamientos) | `bff_web` → `users_management` → `nutrition_habits` + `idempotency_key` + `outbox_event` → `usuarios-outbox-publisher` → `sportapp.usuarios.v1` → `usuarios-read-projector` → `athlete_profile_view.nutrition_habits` + `processed_event`; `training_management` lee las restricciones proyectadas por el contrato interno | OpenAPI 0.10.0 de `users-management` y 0.21.0 de `bff-web`; evento `habitos.alimenticios_actualizados.v1`; ADR-035 |
| HU010 Crear plan de entrenamiento | `POST /v1/me/training-authorizations` y `DELETE /v1/me/training-authorizations/{professional_id}` (deportista); `POST /v1/training-plans` (`Idempotency-Key` obligatorio) y `GET /v1/training-plans/{plan_id}`, todos con access token; internos: `POST /v1/users/lookup` en Usuarios y las mismas rutas por identificador en Entrenamientos | `bff_web` → `users_management` (lookup en la base de lectura) → `training_management` → `training_authorization` / `training_plan` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `training_plan_view` | OpenAPI 0.4.0 de `users-management`, 0.2.0 de `training-management` y 0.6.0 de `bff-web`; evento `plan.entrenamiento_creado.v1` |
| HU011 Ver historial de entrenamiento | `GET /v1/training-history` (deportista, su historial) y `GET /v1/athletes/{athlete_id}/training-history` (profesional autorizado), con `from`, `to`, `sport`, `limit` y `cursor`; CA5: `GET /v1/me/training-athletes?limit&cursor` (profesional; internos `GET /v1/me/authorized-athletes` en Entrenamientos y `POST /v1/users/lookup-batch` en Usuarios) | `bff_web` → `training_management` → `training_session_history` y `training_authorization_view` (base de lectura) ← `entrenamientos-read-projector` ← `entrenamiento.finalizado.v1` (lo publicará HU032) y `autorizacion_entrenamiento.*.v1` (outbox de la autorización) | OpenAPI 0.3.0 de `training-management` y 0.8.0 de `bff-web`; CA5: 0.8.0 de `training-management`, 0.7.0 de `users-management` y 0.15.0 de `bff-web`; eventos `autorizacion_entrenamiento.otorgada.v1`, `autorizacion_entrenamiento.revocada.v1` y `entrenamiento.finalizado.v1` |
| HU014 Crear plan de alimentación | `POST /v1/nutrition-plans` (`service_provider` con `nutrition.plan.author` y autorización vigente; `Idempotency-Key` obligatorio), `GET /v1/nutrition-plans/{plan_id}` (creador o deportista), `GET /v1/me/nutrition-plans` (deportista) y `GET /v1/food-catalog` (`ETag`/`304`) en `bff-web` y `training-management`; interno: `GET /v1/users/{user_id}/nutrition-restrictions` en Usuarios | `bff_web` → `users_management` (lookup) → `training_management` → restricciones en Usuarios (fuera de la transacción) → `nutrition_plan` + comidas + ítems + `idempotency_key` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `nutrition_plan_view` | OpenAPI 0.8.0 de `users-management`, 0.9.0 de `training-management` y 0.18.0 de `bff-web`; evento `plan.alimentacion_creado.v1`; ADR-037 |
| HU015 Crear servicio | `POST /v1/services` (profesional con `services.create`; `Idempotency-Key` obligatorio) | `bff_web` → `complementary_services` → `service` + `service_availability` + `idempotency_key` (base única, sin eventos) | OpenAPI 0.2.0 de `complementary-services` y 0.9.0 de `bff-web`; ADR-027 |
| HU016 Seleccionar o cambiar plan de afiliación | `GET /v1/plans`, `GET /v1/me/subscription` y `POST /v1/me/subscription/changes` (`Idempotency-Key` obligatorio, `expected_subscription_version`), solo `athlete`, en `bff-web` y `affiliation-management` | `bff_web` → `affiliation_management` (JWKS de Usuarios) → bloqueo de la suscripción activa → `subscription` (termina la anterior y crea la nueva) + `billing_commitment` + `subscription_history` + `command_idempotency` + `outbox_event` → `afiliacion-outbox-publisher` → `sportapp.afiliacion.v1` (sin consumidores); `GET /v1/me/capabilities` lee la suscripción nueva en línea | OpenAPI 0.3.0 de `affiliation-management` y 0.20.0 de `bff-web`; evento `suscripcion.cambiada.v1`; ADR-036 y ADR-024 |
| HU007 Consultar estado de cuenta | `GET /v1/me/account-statement?limit&cursor` (`athlete`, `organizer` y `service_provider`; solo la cuenta del token) en `bff-web` y `affiliation-management` | `bff_web` → `affiliation_management` (JWKS de Usuarios) → lectura `REPEATABLE READ` de `subscription`, `plan_offer`, `billing_commitment` y `subscription_history` (sin escritura ni eventos) | OpenAPI 0.4.0 de `affiliation-management` y 0.25.0 de `bff-web`; ADR-036 |
| HU019 Publicar servicios | `POST /v1/services/{service_id}/publish` (profesional propietario con `services.publish`; sin cuerpo; `Idempotency-Key` obligatorio), `GET /v1/me/services?status&limit&cursor` y `GET /v1/me/services/{service_id}` (solo `service_provider`) | `bff_web` → `complementary_services` → bloqueo de `service` + `idempotency_key` + `outbox_event` → `servicios-outbox-publisher` → `sportapp.servicios.v1` (sin proyector ni suscripción consumidora) | OpenAPI 0.3.0 de `complementary-services` y 0.11.0 de `bff-web`; evento `servicio.publicado.v1`; ADR-027 y ADR-038 |
| HU020 Visualizar disponibilidad de los servicios | `GET /v1/services/availability?from&to&category&modality&availability&limit&cursor` (solo deportista; `category` y `modality` repetibles) | `bff_web` → `complementary_services` (base única; capacidad médica verificada ante Afiliación solo si la categoría puede aparecer) → `service` + `service_availability`; solo lectura, sin eventos | OpenAPI 0.4.0 de `complementary-services` y 0.12.0 de `bff-web`; ADR-038 |
| HU021 Crear evento masivo | `POST /v1/events` (organizador con `events.create`; `Idempotency-Key` obligatorio) | `bff_web` → `events_management` → `event` + `command_idempotency` + `outbox_event` → `eventos-outbox-publisher` → `sportapp.eventos.v1` → `eventos-read-projector` → `event_read` + `processed_event` | OpenAPI 0.2.0 de `events-management` y 0.10.0 de `bff-web`; evento `evento.creado.v1`; ADR-028 |
| HU026 Sugerir rutas | Web (`organizer` con `training.routes.author` de `actor-permissions.v2`): `POST /v1/routes` (`Idempotency-Key` obligatorio), `POST /v1/routes/{route_id}/retire` (`expected_version`) y `GET /v1/me/routes` en `bff-web` y `training-management`; móvil (`athlete` con `training.routes.suggest`): `GET /v1/me/suggestions/routes?limit` en `bff-mobile` (en el servicio, además `sport` repetible, `city` y `country_code`); interno: `GET /v1/users/{user_id}/athlete-context` en Usuarios | `bff_web` → `training_management` → `route` + `command_idempotency` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `route_view`; `bff_mobile` → Afiliación (capacidad) → `users_management` (`athlete-context` y `directory-lookup`) → `training_management` (`route_view`) | OpenAPI 0.11.0 de `training-management`, 0.12.0 de `users-management`, 0.23.0 de `bff-web` y 0.10.0 de `bff-mobile`; eventos `ruta.publicada.v1` y `ruta.retirada.v1`; migraciones `0007_routes` (escritura) y `0009_route_view` (lectura); `contracts/authorization/actor-permissions.v2.json`; ADR-041 |
| HU028 Sugerir planes de entrenamiento | Web (`service_provider` con `training.plan.author`): `POST /v1/training-plan-templates` (`Idempotency-Key` obligatorio), `POST /v1/training-plan-templates/{template_id}/retire` (`expected_version`) y `GET /v1/me/training-plan-templates` en `bff-web` y `training-management`; móvil (`athlete` con `training.plans.suggest`): `GET /v1/me/suggestions/training-plan-templates?limit` en `bff-mobile` (en el servicio, además `sport` repetible y `primary_goal`) | `bff_web` → `training_management` → `training_plan_template` + `command_idempotency` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `training_plan_template_view`; `bff_mobile` → Afiliación (capacidad) → `users_management` (`athlete-context` y `directory-lookup`) → `training_management` | OpenAPI 0.12.0 de `training-management`, 0.24.0 de `bff-web` y 0.11.0 de `bff-mobile`; eventos `plantilla_entrenamiento.publicada.v1` y `.retirada.v1`; migraciones `0008_training_plan_templates` (escritura) y `0010_training_plan_template_view` (lectura); ADR-041 |
| HU032 Iniciar o finalizar entrenamiento | `POST /v1/training-sessions`, `POST /v1/training-sessions/{session_id}/finish` y `GET /v1/training-sessions/active` (deportista; `Idempotency-Key` obligatorio en los comandos) | `bff_mobile` → `training_management` (capacidad en Afiliación solo al iniciar) → `training_session` + `command_idempotency` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `active_training_session` y `training_session_history` | OpenAPI 0.4.0 de `training-management` y 0.5.0 de `bff-mobile`; eventos `entrenamiento.iniciado.v1` y `entrenamiento.finalizado.v1`; ADR-029 |
| HU037 Conectar dispositivos de medición | `POST /v1/me/devices` (`Idempotency-Key` obligatorio), `GET /v1/me/devices` y `DELETE /v1/me/devices/{device_id}` (`athlete` con `devices.measurement.connect`); `POST /v1/training-sessions/{session_id}/measurements` (`athlete` con `training.realtime.monitor`; sin clave: idempotencia natural) | `bff_mobile` → Afiliación (capacidad) → `training_management` (capacidad de nuevo, fuera de la transacción) → `athlete_device` + `command_idempotency`; la ingestión → `training_measurement` (clave `session_id, device_id, measured_at`), sin eventos propios | OpenAPI 0.13.0 de `training-management` y 0.12.0 de `bff-mobile`; migración de escritura `0009_devices_measurements`; ADR-044 |
| HU033 Ver tablero de entrenamientos | `GET /v1/me/training-dashboard?from&to` (solo `athlete` con `training.dashboard.view`; instantes con desfase; por defecto 30 días; como mucho 366) | `bff_mobile` → Afiliación (capacidad) → `training_management` → agrupación por deporte y semana ISO de `training_session_history` y último resultado por indicador de `training_metric_result` (base de lectura, sin vista materializada) | OpenAPI 0.10.0 de `training-management` y 0.9.0 de `bff-mobile`; sin eventos ni migraciones; ADR-043 |
| HU034 Calcular indicadores FTP y VO₂max | `GET /v1/training-metrics/catalog` (`ETag`/`304`), `POST /v1/training-metrics/calculations` (`Idempotency-Key` obligatorio) y `GET /v1/training-metrics/calculations?metric_code&limit&cursor` en `bff-mobile` y `training-management` (deportista con `training.metrics.ftp_vo2max`) | `bff_mobile` → `training_management` (capacidad en Afiliación, fuera de la transacción) → `training_metric_calculation` + `training_metric_input` + `command_idempotency` + `outbox_event` → `entrenamientos-outbox-publisher` → `sportapp.entrenamientos.v1` → `entrenamientos-read-projector` → `training_metric_result` + `processed_event` | OpenAPI 0.6.0 de `training-management` y 0.8.0 de `bff-mobile`; evento `indicador.calculado.v1`; ADR-030 |
| HU035 Ver calendario con eventos o salidas programadas | `GET /v1/me/calendar?from&to` en `bff-mobile` (deportista con `calendar.sport.view`); internos: `GET /v1/events?from&to&limit&cursor` en Eventos y `GET /v1/me/training-plan-sessions?from&to&limit&cursor` en Entrenamientos | `bff_mobile` compone en paralelo `events_management` (`event_read`, solo `PUBLISHED`; capacidad verificada de nuevo en Afiliación) y `training_management` (`training_plan_session_view`, proyectada desde `plan.entrenamiento_creado.v1`); solo lectura | OpenAPI 0.3.0 de `events-management`, 0.5.0 de `training-management` y 0.6.0 de `bff-mobile`; sin eventos nuevos; ADR-031 |
| HU024 Publicar eventos deportivos | `POST /v1/events/{event_id}/publish` (sin cuerpo; `Idempotency-Key` obligatorio; `organizer` propietario con `events.publish`), `GET /v1/me/events?status&limit&cursor` (eventos propios del `organizer`) y `GET /v1/events/{event_id}` (`PUBLISHED` para cualquier actor autenticado, `DRAFT` solo para su organizador; `bff-web` sin `my_registration`) | `bff_web` → `events_management` → `event` bloqueado + `command_idempotency` + `outbox_event` → `eventos-outbox-publisher` → `sportapp.eventos.v1` → `eventos-read-projector` (estado completo, versión mayor) → `event_read` + `processed_event` | OpenAPI 0.5.0 de `events-management` y 0.16.0 de `bff-web`; evento `evento.publicado.v1`; ADR-033 |
| HU022 Consultar usuarios inscritos | `GET /v1/me/events/{event_id}/registrations?status&limit&cursor` (organizador propietario con `events.attendees.view`) en `bff-web` y `events-management`; interno: `POST /v1/users/directory-lookup` en Usuarios (solo `full_name`) | `bff_web` → `events_management` (`event_registration_read` de HU041, propiedad en `event_read`) + `users_management` (`user_directory`) para `display_name`; solo lectura | OpenAPI 0.7.0 de `events-management`, 0.11.0 de `users-management` y 0.22.0 de `bff-web`; migración de lectura `0006_registration_event_order`; sin eventos; ADR-039 |
| HU023 Consultar calendario de eventos | `GET /v1/events?from&to&discipline&city&limit&cursor` (`organizer` con `events.calendar.view`; solo `PUBLISHED`, sin `organizer_id`) en `bff-web` y `events-management`; `GET /v1/me/events/{event_id}/conflicts` (composición propia de `bff-web`) | `bff_web` → `events_management` (`event_read`; las coincidencias leen `GET /v1/me/events` y dos consultas de `GET /v1/events`) | OpenAPI 0.6.0 de `events-management` y 0.17.0 de `bff-web`; sin eventos ni migraciones; ADR-040 |
| HU041 Inscribirse a un evento publicado | `GET /v1/events/{event_id}` (detalle `PUBLISHED` con `my_registration`), `POST /v1/events/{event_id}/registrations` (sin cuerpo; `Idempotency-Key` obligatorio) y `GET /v1/me/event-registrations` (deportista; sin capacidad de plan) en `bff-mobile` y `events-management` | `bff_mobile` → `events_management` → bloqueo de `event` + `event_registration` + `command_idempotency` + `outbox_event` → `eventos-outbox-publisher` → `sportapp.eventos.v1` → `eventos-read-projector` → `event_registration_read` + `processed_event` | OpenAPI 0.4.0 de `events-management` y 0.7.0 de `bff-mobile`; evento `usuario.inscrito.v1`; ADR-032 |

Detalles de HU001:

- **Correo:** se normaliza (minúsculas, sin espacios exteriores) y es único.
- **Contraseña:** 12–128 caracteres, sin reglas de composición; se guarda solo como hash Argon2id (m=19 MiB, t=2, p=1).
- **`actor_type`:** `athlete`, `organizer` o `service_provider`.
- **Políticas:** `accepted_policies_version` debe coincidir con `REGISTRATION_POLICIES_VERSION`; si no, `422 policies_version_mismatch`. La versión compartida aprobada el 2026-10-17 es `sportapp-c108-c108plus.v1`: los Convenios 108 y 108+ del Consejo de Europa son el marco de referencia de protección de datos. SportApp no declara estar certificada, y el tratado no sustituye su aviso de privacidad propio. La cuenta conserva solo la versión aceptada y su instante, nunca el texto de la política.
- **Idempotencia:** un reintento con la misma clave y el mismo cuerpo, durante 24 h, devuelve el mismo `201` con `Idempotent-Replayed: true`.
- **Alta gratuita (ADR-024, criterios 6-9):** Afiliación consume `usuario.registrado.v1` (suscripción `sportapp.afiliacion.alta-gratuita.v1`) y, solo para `athlete`, crea una suscripción `FREE` activa con origen `AUTO_REGISTRATION`. `organizer` y `service_provider` no reciben suscripción. Cada evento queda en `inbox_event`; una entrega repetida no crea otra suscripción y un índice único parcial impide dos suscripciones activas por usuario. Inbox, suscripción y outbox se confirman juntos y el mensaje se confirma (ACK) después del COMMIT; un mensaje inválido no se confirma y, tras 10 intentos, va a la DLQ `sportapp.afiliacion.alta-gratuita.dlq`. La activación publica `suscripcion.activada.v1` en `sportapp.afiliacion.v1` con `user_id` como clave de orden.
- **Catálogo:** la migración `0002_free_plan_catalog` precargó `plans-2026-10-11.v1`: `FREE` (11 capacidades), `INTERMEDIATE` (14) y `PREMIUM` (16), con los planes pagos sin precio. La migración `0003_plan_price_offers` (ADR-036) instala `plans-2026-10-13.v1` sin tocar la anterior:
  - versión 2 de los tres planes, con las mismas capacidades; las versiones 1 no cambian, los deportistas existentes las conservan y los nuevos reciben `FREE` v2;
  - `INTERMEDIATE` y `PREMIUM` activos y seleccionables;
  - tabla `plan_offer` con siete ofertas en COP, precios de referencia antes de impuestos: `FREE` `NONE` 0; `INTERMEDIATE` 2.400 / 7.200 / 28.800 y `PREMIUM` 4.800 / 14.400 / 57.600 (mensual, trimestral, anual);
  - `catalog_metadata.migration_revision` para auditar qué migración instaló cada catálogo.

  La precarga es idempotente y corre en el Job de migración antes de habilitar las réplicas. No existe ninguna API ni pantalla para administrar planes. Afiliación no queda lista (`/health/ready` 503) si falta la versión esperada, un plan, una capacidad o una oferta (`plan_offers_mismatch:<plan>`); por eso un despliegue que siga esperando `plans-2026-10-11.v1` tampoco queda listo.
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
scripts/release.py             release: verify, k8s-check, images, push, manifest
.github/workflows/             ci.yml y release-validation.yml (sin acceso a GCP)
cloudbuild.release.yaml        construcción y publicación en Cloud Build (iniciada desde GCP)
deploy/cloudbuild/             uv fijado por hash para Cloud Build
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
| Carga HA014 (fuera de cada PR) | `uv run --group load locust -f load/locust/training_metrics.py --host http://127.0.0.1:8000 --headless` (parámetros y dataset en la cabecera del script) |
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

Detalles de HU004 (decisiones y contrato aprobados el 2026-10-12):

- **Qué revoca:** toda la sesión (familia) del refresh token presentado, incluidos los tokens ya rotados y el sucesor de una renovación en curso: la revocación bloquea la familia antes de actualizarla. Otras sesiones del mismo usuario siguen activas. Sin migración: usa `refresh_token.revoked_at` de HU003.
- **Idempotente y sin revelar nada:** siempre `204`, también con un token desconocido, vencido o ya revocado; repetir el cierre no cambia la fecha de revocación ni reactiva la sesión. Después, `POST /v1/sessions/refresh` responde `401 invalid_refresh_token`.
- **Access token:** no se invalida; expira por su vigencia corta (≤ 15 min, HU003). No se agregó ningún claim ni consulta de revocación.
- **Web:** `POST /v1/sessions/revoke` sin cuerpo lee la cookie `__Secure-sportapp_refresh` (su `Path=/v1/sessions` ya la envía) y exige un `Origin` permitido (`403 origin_not_allowed`, sin tocar la cookie). Responde `204` y borra la cookie, también sin cookie. Si Usuarios no responde (`503`/`504`) **también borra la cookie**: el cierre local queda garantizado, aunque la revocación remota no se confirmó.
- **Móvil:** `POST /v1/sessions/revoke` con `{"refresh_token": "..."}` y `Idempotency-Key` (UUID) **opcional**; `204`, `422 validation_error`, `503 upstream_unavailable` o `504 upstream_timeout`. Con `503`/`504` la aplicación guarda la credencial en su cola cifrada de revocación y reintenta (ADR-026, CA6-CA7) repitiendo la misma clave. La clave no se persiste (el token ya hace idempotente la revocación); se registra en la auditoría para correlacionar reintentos. Acepta cualquier refresh token válido: revocar nunca amplía acceso; el rechazo de actores no deportistas al iniciar sesión sigue pendiente en HU003.
- **Auditoría (CA4):** `audit.session_revoke` con `result` (`revoked`, `already_revoked` o `unknown`), `user_id`, `family_id`, `idempotency_key` si llegó, y la fecha del registro; nunca el token ni su hash.
- **Sin evento:** `credencial.revocada.v1` sigue siendo un candidato sin consumidores.

Detalles de HU005 (ADR-034; decisiones de la tarea aprobadas el 2026-10-16):

- **Quién:** solo `athlete`, con la identidad del token; `users-management` vuelve a verificar el access token con su propio JWKS (sin llamada de red). `organizer` y `service_provider` → `403 permission_required`. No depende del plan.
- **Secciones:** demográfica (`birth_date` con edad de 13 a 120 años, `gender`, `country_code` ISO alfa-2, `city`; opcionales `marital_status`, `occupation`, `income_range`) y fisiológica (`consent.version` = `HEALTH_DATA_CONSENT_VERSION`, `measured_at` `AAAA-MM-DDTHH:MM:SSZ` entre −365 días y +5 min, `weight_kg` y `body_temperature_c` como cadena con un decimal, `height_cm`, opcionales `resting_heart_rate_bpm` y `blood_pressure`). Sin raza/etnia, religión ni magnitudes clínicas excluidas. `bmi` lo calcula el backend (`Decimal`, `ROUND_HALF_UP`: 68.5 kg y 172 cm → `"23.2"`) y se rechaza en el cuerpo.
- **Validación todo o nada:** `422 validation_error` con `details[{field, issue}]` y el catálogo de ADR-034; cualquier campo fuera del contrato (incluidos `user_id` y `bmi`) → `field_not_allowed`; la `Idempotency-Key` ausente o no UUID se informa junto con el resto.
- **Versión y concurrencia:** `201` al crear y `200` al reemplazar; la sección se bloquea (`FOR UPDATE`) y `expected_version` debe coincidir: si no, `409 version_conflict` con `details=[{field: expected_version, issue: "<versión vigente>"}]` (`"0"` si la sección no existe). Ocho reemplazos simultáneos producen una sola versión nueva.
- **Idempotencia:** tabla `idempotency_key` existente, `operation` propia por sección, 24 h; el reintento devuelve el mismo código y cuerpo con `Idempotent-Replayed: true`; otro payload u otro deportista con la misma clave → `409 idempotency_key_reused`.
- **Transacción y eventos:** sección + clave + `outbox_event` en un COMMIT (migración de escritura `0004_athlete_profile`); `perfil.demografico_actualizado.v1` y `perfil.fisiologico_actualizado.v1` con el estado completo de la sección, `ordering_key = user_id` y `causation_id` = clave.
- **Consulta:** `GET /v1/me/profile` lee `athlete_profile_view` (migración de lectura `0003_athlete_profile_view`): siempre `200`, con secciones y versiones `null` si aún no se proyectan; si la base de lectura cae, `503 upstream_unavailable`. Es eventual: el cliente usa la respuesta del comando como confirmación inmediata.
- **Proyector (`usuarios-read-projector`, CMP-001):** `python -m users_management.workers.read_projector`; suscripción `proyector-usuarios-v1` con orden, DLQ `sportapp.usuarios.proyeccion.dlq` (5 entregas, espera de 10 a 600 s) y `…dlq.ops`. `usuario.registrado.v1` → `user_directory` (solo la siguiente versión: desde ahora `POST /v1/users/lookup` resuelve a usuarios reales); `perfil.*` → la sección de `athlete_profile_view` si la versión es mayor; deduplica por `event_id` en `processed_event` en la misma transacción y confirma después del COMMIT; otros tipos se confirman sin efectos.
- **Privacidad:** ningún valor demográfico ni fisiológico en logs, auditoría, métricas ni errores. Auditoría `audit.profile_section_updated` con `user_id`, `section`, `section_version`, `result` y `correlation_id` (el nombre `section_version` evita que el formateador de logs pise la `version` del servicio).
- **Tópico compartido (R-19, parte backend):** el publicador agrega `aggregate_type` a los atributos del mensaje; la suscripción `sportapp.afiliacion.alta-gratuita.v1` del emulador filtra `attributes.event_type = "usuario.registrado.v1"` y el consumidor `FREE` confirma sin efectos los demás tipos. Un emulador ya en marcha conserva la suscripción sin filtro: reinícielo para aplicarlo. El filtro en GCP lo debe crear la plataforma (`T-HU001-PLAT`).

Detalles de HU009 (ADR-035, aceptada el 2026-10-12; decisiones de la tarea del 2026-10-24):

- **Quién:** solo `athlete`, sin capacidad de plan, filtrado en `bff-web` y de nuevo en el servicio (`403 permission_required`). La identidad sale del token; `user_id` en el cuerpo → `422 field_not_allowed`. Un profesional nunca lee los hábitos (CA4): `bff-web` no expone el contrato interno y solo recibe el resultado de la validación de un plan (HU014).
- **Cuerpo:** `consent.version` obligatorio e igual a `HEALTH_DATA_CONSENT_VERSION` (alergias y restricciones son datos de salud); `diet_pattern` (`OMNIVORE`, `VEGETARIAN`, `VEGAN`, `PESCATARIAN`, `FLEXITARIAN`); `restrictions` de 0 a 7 valores únicos (`GLUTEN_FREE`, `LACTOSE_FREE`, `LOW_SODIUM`, `LOW_SUGAR`, `KETO`, `HALAL`, `KOSHER`); `allergies` de 0 a 9 valores únicos (`PEANUTS`, `TREE_NUTS`, `MILK`, `EGGS`, `WHEAT`, `SOY`, `FISH`, `SHELLFISH`, `SESAME`); `meals_per_day` 1–8; `hydration_liters_per_day` opcional, cadena con hasta un decimal entre `0.5` y `10.0` (se normaliza a un decimal; un número JSON es `unknown_value`). Ambas listas son obligatorias y pueden ir vacías. Sin texto libre.
- **Validación (todo o nada):** `422 validation_error` con `details[{field, issue}]` del catálogo de ADR-034 y la posición recibida (`allergies[1]` `duplicate_value`, `restrictions[0]` `unknown_value`); consentimiento ausente → `required`, distinto → `unsupported_consent_version`. Ningún `422` deja datos ni evento.
- **Normalización canónica (implementación interna):** restricciones y alergias se devuelven en el orden de su catálogo; la misma información produce el mismo documento y la misma huella idempotente.
- **Comando:** el flujo de HU005 (`UpdateProfileSection`), con `operation = put_nutrition_habits` (nombre del paquete) y `resource_id = user_id` (24 h). Sección bloqueada (`FOR UPDATE`), clave consultada de nuevo tras el bloqueo, `expected_version` obligatorio al reemplazar (`409 version_conflict`). Sección, clave y evento en un COMMIT. Auditoría `audit.profile_section_updated` con `section = nutrition_habits`, sin valores.
- **Persistencia:** escritura `0006_nutrition_habits` (el paquete sugería `0005`, ocupada por HU006): tabla `nutrition_habits` con PK y FK local `user_id`, consentimiento, `diet_pattern`, listas JSONB, `meals_per_day`, `hydration_liters_per_day NUMERIC(3,1)`, versión y marcas, con restricciones de forma. Lectura `0005_nutrition_habits_view`: columnas `nutrition_habits` y `nutrition_habits_version` en `athlete_profile_view`, sin relleno.
- **Evento y proyección:** `habitos.alimenticios_actualizados.v1` (`aggregate_type = nutrition_habits`, `aggregate_id = ordering_key = user_id`, estado completo, sin otras secciones). `usuarios-read-projector` lo valida contra el contrato (catálogos, orden, decimales) y lo aplica por estado completo, como HU006. La suscripción `alta-gratuita` de Afiliación no lo recibe (filtro de R-19).
- **Contrato interno de restricciones:** desde HU009 lee `athlete_profile_view.nutrition_habits` (base de lectura, eventual) y devuelve solo `diet_pattern`, `restrictions`, `allergies` y `version`. Sin hábitos sigue respondiendo `200` con listas vacías y `null` (contrato 0.8.0 de HU014): el `404 nutrition_habits_not_found` que propone HU009 CA5 rompería Entrenamientos, que trata cualquier `404` como indisponibilidad (decisión del 2026-10-24, por ratificar en el criterio). Una solicitud con `Authorization` (un cliente, si la ruta se expusiera por error) → `403 permission_required` sin leer datos; base de lectura caída → `503 upstream_unavailable`. Auditoría `audit.nutrition_restrictions_read` con `user_id`, `consumer = training-management` (valor fijo: es el único consumidor autorizado y el contrato no lleva token, decisión del 2026-10-24), resultado (`found`, `habits_missing`, `user_not_found`, `unavailable`, `rejected`) y la correlación del log.
- **Pruebas de componente:** `ProfileFlowCase` instala ahora la captura de logs después de crear las apps (`create_service_app` reemplaza los manejadores del log raíz): antes las comprobaciones de minimización de HU005 y HU006 no capturaban nada.
- **Versiones:** `users-management` 0.10.0 y `bff-web` 0.21.0.

Detalles de HU016 (ADR-036 y ADR-024; decisiones de la tarea del 2026-10-23):

- **Quién:** solo `athlete`, sin capacidad de plan, filtrado en `bff-web` y de nuevo en el servicio (`403 permission_required`). Afiliación verifica ahora el access token con el JWKS de Usuarios (decisión del 2026-10-23), igual que Eventos y Servicios: necesita `USERS_MANAGEMENT_BASE_URL`.
- **Consultas:** `GET /v1/plans` devuelve `catalog_version`, `current_plan_code` (`FREE` tras el alta automática, sin selección manual; `null` en `PROVISIONING`) y los tres planes con versión, nivel, capacidades y ofertas (`periodicity`, `price` con dos decimales como cadena, `currency`, `duration_months`). `GET /v1/me/subscription` devuelve la suscripción activa con su oferta, precio y periodo; sin suscripción todavía → `200` con `status: PROVISIONING` y el resto `null` (decisión del 2026-10-23). Ambas con `no-store`.
- **Comando:** cuerpo `{plan_code, periodicity, expected_subscription_version}` (`extra` prohibido). `404 plan_not_found`; `409 plan_not_selectable`; periodicidad incoherente (`NONE` solo para `FREE`; `MONTHLY`/`QUARTERLY`/`ANNUAL` para pagos) → `422 validation_error` `body.periodicity` `inconsistent_values`; mismo plan y periodicidad → `409 subscription_unchanged`; sin suscripción → `409 subscription_provisioning` (recuperable); versión distinta → `409 version_conflict` con la versión vigente en `details`. Ningún rechazo modifica la suscripción.
- **Transacción:** bloqueo (`FOR UPDATE`) de la suscripción activa del usuario y nueva consulta de la clave tras el bloqueo; la anterior pasa a `ENDED` y se crea la nueva `ACTIVE` (versión + 1, `origin = USER_CHANGE`, `period_start` = ahora, `period_end` = `period_start` + meses de calendario de la oferta, ajustado al fin de mes; `null` para `FREE`); cambio inmediato y sin prorrateo. Un plan pago crea un `billing_commitment` `PENDING_EXTERNAL` informativo (importe y moneda de la oferta, `due_at = period_start`). Pago → pago conserva los compromisos anteriores; volver a `FREE` condona (`WAIVED`) los que siguen vigentes (decisión del 2026-10-23). Movimientos `PLAN_CHANGED`/`RETURNED_TO_FREE`, `COMMITMENT_CREATED` y `COMMITMENT_WAIVED` en `subscription_history`, `suscripcion.cambiada.v1` en la outbox y la clave, todo en un COMMIT. El índice único parcial `uq_subscription_active_user` impide dos activas: un choque concurrente → `409 version_conflict`.
- **Idempotencia:** `Idempotency-Key` UUID por deportista y operación `change_subscription` durante 24 h en `command_idempotency`; misma clave y cuerpo → la respuesta original con `Idempotent-Replayed: true`; otro cuerpo → `409 idempotency_key_reused`.
- **Alta gratuita:** el consumidor de HU001 asigna la oferta `NONE` de `FREE` y registra `FREE_ACTIVATED` en `subscription_history` (identificador UUIDv5 de la suscripción).
- **Persistencia:** migración `0004_subscription_changes` (no toca `0003`): columnas `periodicity`, `plan_offer_id` (FK a `plan_offer`), `period_start` y `period_end` en `subscription`; estado `ENDED`, origen `USER_CHANGE`; tablas `billing_commitment`, `subscription_history` y `command_idempotency`. Relleno idempotente de las suscripciones existentes (oferta `NONE`, `period_start = started_at` y `FREE_ACTIVATED`).
- **Evento:** `suscripcion.cambiada.v1` en `sportapp.afiliacion.v1` (`aggregate_type = user_subscription`, `aggregate_id = ordering_key = user_id`, `aggregate_version` = versión de la suscripción nueva, `causation_id = Idempotency-Key`), con identificadores, planes anterior y nuevo, versión, periodicidad, catálogo, estado, origen y periodo; sin importes, compromisos ni datos personales. Sin consumidores aprobados.
- **Capacidades:** `GET /v1/me/capabilities` consulta Afiliación en cada llamada; `bff-web` no guarda caché de capacidades, así que no hay nada que invalidar. Ninguna operación implementada exige todavía una capacidad paga con `403 capability_required`; la prueba de componente de HU020 demuestra con la Afiliación real que pasar a `PREMIUM` habilita la categoría médica y volver a `FREE` la restringe.
- **Auditoría:** `audit.subscription_changed` con `user_id`, planes, periodicidad, `subscription_id`, resultado (`changed`, `replayed`, `rejected` con `reason`) y correlación; sin importes.
- **Versiones:** `affiliation-management` 0.3.0 y `bff-web` 0.20.0.

Detalles de HU007 (ADR-036; decisiones de la tarea del 2026-10-24):

- **Quién:** los tres actores, filtrados en `bff-web` y de nuevo en el servicio; un actor ausente o desconocido → `403 permission_required` (ADR-025). La cuenta sale del token: no existe parámetro de usuario ni consulta administrativa (CA3).
- **`statement_status`:** `organizer` y `service_provider` → `NOT_APPLICABLE` sin leer la base (CA2); deportista con suscripción activa → `ACTIVE`; sin ninguna suscripción (alta `FREE` en curso) → `PROVISIONING`, como `GET /v1/me/subscription`; con suscripciones anteriores pero ninguna activa → `NONE`, la misma lectura de «sin suscripción activa» del contrato interno de capacidades (por ratificar: el cambio de plan nunca deja ese estado). `PROVISIONING` y `NOT_APPLICABLE` llevan `subscription: null` y listas vacías.
- **Compromisos:** los de la suscripción vigente y los anteriores **no cerrados**, entendidos como los que tienen el periodo en curso (`period_end > now`), en cualquier estado (`PENDING_EXTERNAL` o `WAIVED`), del `period_start` más reciente al más antiguo (por ratificar). Los demás siguen visibles como movimientos.
- **Movimientos:** `subscription_history` con keyset opaco (`occurred_at DESC, movement_id DESC`), `limit` 1–100 (20 por defecto); `COMMITMENT_CREATED` y `COMMITMENT_WAIVED` llevan `commitment_id`, `amount` y `currency` de su compromiso, los demás `null` (CA1: moneda clara). `limit` fuera de rango o no entero → `422 query.limit out_of_range`; cursor ilegible → `422 query.cursor invalid_cursor`.
- **Sin pagos:** `payments_status: NOT_INTEGRATED` y `balance: null` siempre (CA4). Importes como cadena con dos decimales e instantes en UTC (`Z`); `no-store`; los importes no se registran en logs.
- **Lectura:** una sola transacción `REPEATABLE READ` (suscripción, compromisos y página de movimientos coherentes ante un cambio concurrente); sin migraciones, escritura ni eventos.
- **Versiones:** `affiliation-management` 0.4.0 y `bff-web` 0.25.0.

Detalles de HU006 (ADR-034, aceptada el 2026-10-12; tarea del 2026-10-23, sin decisiones nuevas):

- **Quién:** solo `athlete`, sin capacidad de plan, filtrado en `bff-web` y de nuevo en el servicio (`403 permission_required`). La identidad sale del token; `user_id` en el cuerpo → `422 field_not_allowed`.
- **Cuerpo:** `sports` de 1 a 5 deportes del catálogo de HU010 (`running`, `cycling`, `swimming`, `strength`, `other`), sin repetir, con `frequency_per_week` 1–7, `session_duration_minutes` 10–600, `intensity` (`LOW`, `MODERATE`, `HIGH`), `level` (`BEGINNER`, `INTERMEDIATE`, `ADVANCED`) y `years_practicing` opcional 0–80. `availability.days` 1–7 únicos de `MON`…`SUN`; `availability.time_slots` 1–4 únicos de `MORNING`, `AFTERNOON`, `EVENING`, `NIGHT`. `primary_goal` opcional (`HEALTH`, `PERFORMANCE`, `WEIGHT_MANAGEMENT`, `RECREATION`, `COMPETITION`).
- **Validación (todo o nada):** `422 validation_error` con `details[{field, issue}]` del catálogo de ADR-034 y la posición recibida en las listas (`sports[1].sport` `duplicate_value`, `availability.days[0]` `unknown_value`). Una lista vacía es `required` y una de más es `too_many_items`. Ningún `422` deja datos ni evento.
- **Normalización canónica (implementación interna):** la respuesta, el evento y la vista devuelven los deportes, los días y las franjas en el orden de su catálogo, sea cual sea el orden recibido. Así la misma información produce el mismo documento y la misma huella idempotente.
- **Comando:** el mismo flujo de HU005 (`UpdateProfileSection`). Sección bloqueada (`FOR UPDATE`), clave consultada de nuevo tras el bloqueo, `expected_version` obligatorio al reemplazar (`409 version_conflict` con la versión vigente, `0` si no existe). `idempotency_key` con `operation = update_sport_habits` y `resource_id = user_id` (24 h). Sección, clave y `habitos.deportivos_actualizados.v1` en un COMMIT. Auditoría `audit.profile_section_updated` con `section = sport_habits`, sin valores. Registrar hábitos no crea planes ni recomendaciones.
- **Persistencia:** migración de escritura `0005_sport_habits` (tabla `sport_habits`: PK y FK local `user_id`, `sports` y `availability` JSONB, `primary_goal`, versión y marcas; restricciones de forma). Sin migración de lectura: `athlete_profile_view` ya tenía `sport_habits` y `sport_habits_version` (HU005).
- **Evento y proyección:** `habitos.deportivos_actualizados.v1` (`aggregate_type = sport_habits`, `aggregate_id = ordering_key = user_id`, estado completo, sin datos demográficos ni fisiológicos). `usuarios-read-projector` lo valida contra el contrato (catálogos y orden canónico) y lo aplica por estado completo: inserta la fila si falta, reemplaza solo con versión mayor y deduplica en `processed_event`. La suscripción `alta-gratuita` de Afiliación no lo recibe (filtro por tipo de R-19). La suscripción de `training_management` queda para HU028.

Detalles de HU037 (ADR-044, aceptada el 2026-10-13; tarea del 2026-10-24):

- **Quién:** solo el `athlete`, en `bff-mobile` y de nuevo en `training-management`. Vincular, listar y revocar exigen `devices.measurement.connect`; la ingestión exige `training.realtime.monitor` (ADR-044 §5). Ambas capacidades son del plan `INTERMEDIATE`; con `FREE` → `403 capability_required`. Afiliación se consulta fuera de toda transacción (`503 upstream_unavailable`, `504 upstream_timeout`).
- **Dispositivo (`athlete_device`, escritura):** un único tipo `HEART_RATE_MONITOR`/`BLE_HRS`. `device_id` es el SHA-256 (64 hexadecimales en minúsculas) de la dirección BLE y el `user_id` que calcula el móvil: la dirección nunca llega al backend (la base lo exige con una restricción). Clave (`athlete_id`, `device_id`); `display_name` 1–60 recortado; estados `ACTIVE`/`REVOKED` con `version` y `revoked_at`. Sin evento ni proyección: `GET /v1/me/devices` lee la base de escritura (activos y revocados, `created_at` y `device_id` descendentes, `no-store`).
- **Vinculación:** `201` + `Location` con el dispositivo `ACTIVE` versión 1; la misma clave (24 horas, `command_idempotency`, operación `register_device`) → `201` con `Idempotent-Replayed: true`; el mismo dispositivo activo con la misma vinculación y otra clave → `200` sin cambios (la clave también recuerda ese resultado); mismo `device_id` activo con otro nombre → `409 device_already_registered` (con la misma clave o con otra); la misma clave con otro dispositivo → `409 idempotency_key_reused`; un dispositivo `REVOKED` vuelve a `ACTIVE` con la versión siguiente (`201`). Errores `422 validation_error` con `details[{field, issue}]`: `required`, `field_not_allowed`, `unknown_value`, `invalid_format` (`device_id`), `invalid_length` (`display_name`), `invalid_uuid` (`Idempotency-Key`).
- **Revocación:** `DELETE` → `200` con `REVOKED` (versión siguiente); repetirla devuelve el mismo estado; ajeno o inexistente → `404 device_not_found`; `device_id` mal formado → `422`.
- **Ingestión (`training_measurement`, escritura):** lote `{device_id, samples[1..60]{measured_at, type: HEART_RATE, value, unit: bpm}}` de una sesión propia `ACTIVE` (HU032) y un dispositivo propio `ACTIVE`. Cada muestra se valida por separado y las inválidas vuelven en `rejected[{index, issue}]` sin rechazar el lote (CA3): `unknown_value`, `field_not_allowed`, `required`, `invalid_unit`, `out_of_range` (`value` fuera de 20–250 o `measured_at` anterior al inicio de la sesión), `invalid_datetime` (UTC `Z` con segundos), `too_far_in_future` (> ahora + 5 s) y `too_old` (> 15 min). Las válidas se insertan con `ON CONFLICT DO NOTHING` sobre (`session_id`, `device_id`, `measured_at`): reenvíos y repetidas del mismo lote cuentan en `duplicates` y se conserva la primera; fuera de orden se aceptan (CA4). Respuesta `200 {session_id, device_id, received, accepted, duplicates, rejected[]}` con `no-store` (CA5); `rejected[].issue` es el esquema enumerado `SampleIssue`. Errores: `404 training_session_not_found` (inexistente o ajena), `404 device_not_found`, `409 training_session_already_finished`, `409 device_revoked` (CA6) y `422 validation_error` para la forma del lote (`session_id`, `device_id`, `samples` `required`/`unknown_value`/`too_many_items`).
- **Concurrencia:** la ingestión toma la sesión y el dispositivo `FOR SHARE`; la revocación y la finalización (`FOR UPDATE`) esperan al lote en curso y los lotes posteriores ven el nuevo estado. Vinculaciones simultáneas del mismo dispositivo crean una sola fila (la clave primaria serializa): con la misma `Idempotency-Key` la perdedora devuelve el resultado de la ganadora con `Idempotent-Replayed: true`; con otra clave se evalúa contra el vínculo que dejó la ganadora.
- **Auditoría:** `audit.device_registered`, `audit.device_revoked` y `audit.measurements_ingested` con `athlete_id`, resultado, motivo y conteos; nunca valores de frecuencia cardiaca, instantes, nombres ni `device_id`.
- **Fuera de alcance (HU036):** evaluación de riesgo (`evaluation` en la respuesta del lote), `medicion.recibida.v1`, `session_monitoring_view`, `monitoring_settings`, `RISK_STOP` y la limpieza de retención de 90 días de `training_measurement`. La carga k6 de 50 sesiones también es de HU036.
- **Por ratificar** (ni el paquete ni ADR-044 los fijan):
  - `409 device_already_registered` también con otra clave (cualquier intento de cambiar el nombre de un vínculo activo) y `200` sin cambios con la misma vinculación y otra clave;
  - reactivar un dispositivo revocado con el mismo `device_id` (de lo contrario el monitor no podría volver a vincularse, porque el hash es determinista);
  - las tres operaciones de dispositivos exigen la capacidad, también revocar y listar;
  - vigencia de 24 horas de la clave y `resource_id` = UUIDv5 del vínculo en `command_idempotency`;
  - códigos de la ingestión: `404 device_not_found` para un dispositivo no vinculado, `409 training_session_already_finished` para una sesión no activa, `out_of_range` para una muestra anterior al inicio y `too_many_items` para más de 60 muestras;
  - `measured_at` con precisión de segundos (como ADR-029); un motivo por muestra con prioridad forma → tipo → unidad → valor → instante.

Detalles de HU028 (ADR-041, aceptada el 2026-10-13; tarea del 2026-10-24):

- **Quién:** publica, retira y lista sus plantillas solo el `service_provider` con el permiso existente `training.plan.author` (sin cambiar `actor-permissions`), en `bff-web` y de nuevo en `training-management`; recibe sugerencias solo el `athlete` con `training.plans.suggest` del plan `FREE`, verificada en `bff-mobile` y en el servicio ante Afiliación. Una plantilla **no es un plan asignado** (HU010): no tiene deportista ni fechas, y sugerirla no la asigna, no la inicia ni la copia como plan personal.
- **Plantilla (`training-management` 0.12.0):** `title` 3–120 (espacios colapsados), `objectives` texto 20–2.000, `sport` del catálogo de HU010, `goal` del catálogo de ADR-034 (`HEALTH`, `PERFORMANCE`, `WEIGHT_MANAGEMENT`, `RECREATION`, `COMPETITION`), `level` `BEGINNER`/`INTERMEDIATE`/`ADVANCED`, `duration_weeks` 1–52 y `weekly_sessions` 1–14 `{day_of_week (MON…SUN, días de ADR-034), description 1–500, duration_minutes 10–600}` en el orden del profesional. Toda violación es `422 validation_error` con `details[{field, issue}]` (CA4) y nada se publica. Nace `PUBLISHED` en versión 1.
- **Idempotencia y retiro:** mismo patrón que las rutas de HU026: `command_idempotency` (`operation = publish_training_plan_template`, 24 horas; reintento → `201` con `Idempotent-Replayed: true`, otra plantilla con la misma clave → `409 idempotency_key_reused`); retiro con la fila bloqueada: ajena o inexistente → `404 template_not_found` (CA3), ya retirada → `200` sin otro evento, `expected_version` vencido → `409 version_conflict` con la versión vigente.
- **Eventos y proyección:** `plantilla_entrenamiento.publicada.v1` y `.retirada.v1` (`aggregate_type = training_plan_template`, `ordering_key = template_id`, estado completo) → `training_plan_template_view` por versión mayor. Migraciones `0008_training_plan_templates` y `0010_training_plan_template_view` (índices de sugerencia por estado y deporte, del profesional y de las últimas publicadas).
- **Sugerencias en el servicio:** `GET /v1/me/suggestions/training-plan-templates?sport&primary_goal&limit`. Con deportes: plantillas `PUBLISHED` de esos deportes, primero las de `goal == primary_goal`, luego `published_at` y `template_id` descendentes; `reasons` `SPORT_MATCH` y `GOAL_MATCH` (CA1). **Con deportes y sin objetivo**, la lista sigue personalizada por deporte (decisión del responsable del 2026-10-24 ante CA2, que menciona «sin deportes u objetivo»: prevalece la regla de ADR-041 «`SPORT_MATCH` obligatorio para personalizar, `GOAL_MATCH` prioriza»). Sin deportes: `personalized: false`, las últimas publicadas sin motivos. Una retirada nunca se sugiere (CA3).
- **`bff-mobile` 0.11.0:** `GET /v1/me/suggestions/training-plan-templates?limit` con la misma caché del contexto que las rutas (una sola por aplicación). Respuesta `{items[], personalized, missing_context[], generated_at, sources[]}`; cada ítem con `reference {type: "training_plan_template", id}`, `title`, `summary` (objetivos), `reasons`, `template_id`, `professional_id` (para el enlace informativo «Autorizar a este profesional» de HU010), `sport`, `goal`, `level`, `duration_weeks`, `weekly_sessions`, `published_at` y `author_name` (CA4). `missing_context` lista `sports` y `primary_goal` cuando faltan; si Usuarios no responde, ambos, `personalized: false` y `sources: [{source: "users", status: "UNAVAILABLE"}]` (CA2).
- **Auditoría:** `audit.template_published` y `audit.template_retired` con `professional_id`, `template_id`, resultado y motivo; sin títulos ni descripciones.
- **Rendimiento (HA010):** con 200 plantillas en la vista, p95 < 2 s en local (`test_p95_below_two_seconds_with_200_templates`); el orden SQL coincide con el del dominio.
- **Por ratificar** (el paquete y ADR-041 no los fijan):
  - vigencia de la clave de publicación (24 horas) y retiro sin `Idempotency-Key` (igual que HU026);
  - la lista no personalizada usa `limit` (1–50, 20 por defecto) sin el tope fijo de 20 de las rutas, porque ADR-041 no lo fija para plantillas;
  - `description` de cada sesión obligatoria (1–500) y `day_of_week` con los días de ADR-034;
  - desempate por `template_id` descendente;
  - forma del ítem móvil (`reference {type: "training_plan_template", id}`, `title`, `summary` = objetivos) y `professional_id` en el ítem para el enlace informativo;
  - nombres de auditoría `audit.template_published` y `audit.template_retired`.

Detalles de HU026 (ADR-041, aceptada el 2026-10-13; tarea del 2026-10-24):

- **Quién:** publica, retira y lista sus rutas solo el `organizer` con el permiso por actor nuevo `training.routes.author` (`contracts/authorization/actor-permissions.v2.json`, canal web), filtrado en `bff-web` y de nuevo en `training-management` (`403 permission_required`). La v2 solo agrega ese permiso: los de la v1 no cambian (también se conserva `training.routes.recommend` del organizador en móvil, que no se usa porque el móvil rechaza al organizador). La v1 sigue en el repositorio; la imagen y el código leen la v2. Recibe sugerencias solo el `athlete` con la capacidad `training.routes.suggest` del plan `FREE`, verificada en `bff-mobile` y de nuevo en el servicio ante Afiliación.
- **Ruta (`training-management` 0.11.0):** `name` 3–120 (espacios colapsados), `description` opcional ≤ 2.000 (vacía → `null`), `sport` del catálogo de HU010, `distance_km` cadena con un decimal entre `0.1` y `500.0` (`12` → `12.0`; con más decimales o fuera de rango → `out_of_range`), `difficulty` `LOW`/`MODERATE`/`HIGH`, `city` 2–80, `country_code` ISO alfa-2 asignado (`unknown_value`), `start_point {lat, lng}` obligatorio (−90..90, −180..180, números, no texto) y hasta 200 `waypoints`. Toda violación es `422 validation_error` con `details[{field, issue}]` y nada se publica (CA2). Nace `PUBLISHED` en versión 1 (CA1); la respuesta trae `organizer_id`, `source: "ORGANIZER"`, `status`, `version` e instantes en UTC `Z`. Sin proveedor cartográfico, GPX, moderación ni valoración de seguridad.
- **Idempotencia y concurrencia:** la publicación usa `command_idempotency` (PK `actor_id`, `operation = publish_route`, `key`) vigente 24 horas: el reintento devuelve el mismo `201` con `Idempotent-Replayed: true`; la misma clave con otra ruta, `409 idempotency_key_reused`; la clave de otro organizador no interfiere. El retiro bloquea la fila (`FOR UPDATE`): ruta inexistente o ajena → `404 route_not_found` (CA4); ya retirada → `200` con su estado, sin otro evento; `expected_version` distinto de la vigente → `409 version_conflict` con `details=[{field: expected_version, issue: <vigente>}]`; si no, `RETIRED` con la versión siguiente. La fila se conserva.
- **Eventos y proyección:** `ruta.publicada.v1` y `ruta.retirada.v1` en `sportapp.entrenamientos.v1` (`aggregate_type = route`, `ordering_key = route_id`), con el estado completo de la ruta; `causation_id` es la `Idempotency-Key` al publicar y el `route_id` al retirar. `entrenamientos-read-projector` los aplica en `route_view` solo si la versión es mayor (un retiro que llega antes que su publicación no se deshace) y deduplica por `event_id`. Migraciones `0007_routes` (escritura) y `0009_route_view` (lectura, con índices `(status, sport, city_key)`, `(organizer_id, created_at DESC, route_id DESC)` y `(status, published_at DESC, route_id DESC)`); `city_key` es la ciudad recortada sin mayúsculas ni acentos.
- **Mis rutas:** `GET /v1/me/routes?limit&cursor` lee `route_view` (todas las propias, publicadas y retiradas), orden `created_at` desc y `route_id` desc, `limit` 1–50 (20 por defecto), cursor keyset opaco; refleja un comando cuando el proyector aplica su evento.
- **Sugerencias en el servicio:** `GET /v1/me/suggestions/routes?sport&city&country_code&limit` (`sport` repetible). Con deportes: rutas `PUBLISHED` de esos deportes, primero las de la ciudad (y del país, si llega), luego `distance_km` y `route_id` ascendentes, con `reasons` `SPORT_MATCH` y `CITY_MATCH` (CA3). Sin deportes: `personalized: false`, las últimas publicadas (`published_at` desc) sin motivos y como mucho 20 aunque `limit` sea mayor. Una retirada nunca se sugiere (CA4). `422` en `query.limit`, `query.sport` (desconocido o repetido), `query.city` (`invalid_length`) y `query.country_code` (`unknown_value`), antes de consultar Afiliación.
- **`bff-mobile` 0.10.0:** el cliente solo envía `limit`. El BFF consulta `GET /v1/users/{user_id}/athlete-context` (contrato interno nuevo de `users-management` 0.12.0) con timeout de 300 ms y caché de 5 minutos por deportista (solo respuestas correctas; en memoria, acotada), pasa deportes, ciudad y país como filtros y resuelve `author_name` con `POST /v1/users/directory-lookup` (ADR-039). Respuesta: `{items[], personalized, missing_context[], generated_at, sources[]}`; cada ítem con `reference {type: "route", id}`, `title` (nombre), `summary` (descripción), `reasons`, los campos de la ruta (sin `organizer_id`) y `author_name`. `missing_context` lista `sports` y `city` cuando faltan en el perfil; si Usuarios no responde, la lista se entrega no personalizada con ambos y `sources: [{source: "users", status: "UNAVAILABLE"}]` (HA011); si falla el directorio, `author_name: null` con la fuente en `UNAVAILABLE`. Si falla Entrenamientos, `503`/`504`. `Cache-Control: no-store`.
- **Contexto interno (`users-management` 0.12.0):** `GET /v1/users/{user_id}/athlete-context` → `{sports[], primary_goal|null, city|null, country_code|null, birth_date|null}` desde `athlete_profile_view` (HU005 y HU006) para una cuenta activa de `user_directory`; sin perfil, `sports: []` y `null`; inexistente o deshabilitada → `404 user_not_found`; con `Authorization` → `403 permission_required` sin leer (como el contrato de restricciones de HU009); `no-store`; auditoría `audit.athlete_context_read` con el consumidor `bff-mobile` y el resultado, sin valores. Lo reutilizarán HU027 a HU030, HU036 y HU039.
- **Auditoría:** `audit.route_published` y `audit.route_retired` con `organizer_id`, `route_id`, resultado y motivo; sin nombres ni coordenadas.
- **Rendimiento (HA010):** con 500 rutas en `route_view`, el caso de uso completo de sugerencias queda con p95 < 2 s en local (prueba `test_p95_below_two_seconds_with_500_routes`); el orden SQL coincide con el del dominio (`test_sql_order_matches_the_domain_order`).
- **Por ratificar** (el paquete y ADR-041 no los fijan; se implementaron con el precedente más cercano):
  - vigencia de la clave de publicación (24 horas, como HU014 y HU016);
  - retiro sin `Idempotency-Key`: una ruta ya retirada devuelve `200` con su estado aunque `expected_version` no coincida, y un `expected_version` vencido sobre una ruta publicada da `409 version_conflict` con la versión vigente (formato de HU005);
  - la lista no personalizada acota a 20 aunque `limit` sea mayor (ADR-041: «últimas 20»); `limit` 1–50 con 20 por defecto;
  - `CITY_MATCH` compara la ciudad sin mayúsculas ni acentos y exige el mismo país cuando el contexto lo trae; sin ciudad en el perfil, la lista sigue personalizada por deporte con `missing_context: ["city"]`;
  - forma de cada ítem móvil (`reference {type: "route", id}`, `title` = nombre, `summary` = descripción) y `missing_context` `["sports", "city"]` cuando Usuarios no responde o la cuenta no se resuelve (`404`);
  - `sources[]` solo con la fuente `users`, que pasa a `UNAVAILABLE` si falla el contexto o el directorio;
  - el contrato interno `athlete-context` sin token, con el consumidor fijo `bff-mobile` en la auditoría y el mismo rechazo de `Authorization` que el de restricciones;
  - nombres de auditoría `audit.route_published`, `audit.route_retired` y `audit.athlete_context_read`.

Detalles de HU033 (ADR-043, aceptada el 2026-10-13; decisiones de la tarea del 2026-10-24):

- **Quién:** solo `athlete` con `training.dashboard.view` (plan `FREE`), verificada en `bff-mobile` y de nuevo en el servicio ante Afiliación; otro actor → `403 permission_required`, sin la capacidad → `403 capability_required`, Afiliación caída o lenta → `503`/`504`. El tablero solo responde para el deportista del token (no hay parámetro de deportista); el profesional usa el historial de HU011 web.
- **Periodo:** `from`/`to` son instantes ISO con desfase (`2026-10-01T00:00:00-05:00`, `Z` o `±HH:MM`); periodo `[from, to)`, por defecto los últimos 30 días en UTC, como mucho 366 días. Errores `422 validation_error` con `details` en `query.from`/`query.to`: `required` (solo llegó uno, como HU035), `invalid_datetime` (sin desfase o mal formado), `ends_before_starts` (`to` ≤ `from`) y `out_of_range` (más de 366 días). El `+` de un desfase positivo debe ir codificado (`%2B`); si llega decodificado como espacio, se interpreta como `+`. Una sesión pertenece al periodo por su `ended_at` (como el historial de HU011) y un indicador por su `measured_at`.
- **Respuesta:** `period {from, to}`; `has_data` (sesiones o indicadores en el periodo); `totals {sessions_count, total_duration_minutes, avg_duration_minutes, distance_meters}`; `by_sport[]` en el orden del catálogo de deportes; `by_week[] {week_start, ...}` con el lunes de la semana ISO en la zona del `from`, ascendente y solo con las semanas que tienen sesiones; `by_source {SPORTAPP, STRAVA}` con `sessions_count` y `total_duration_minutes` (decisión del 2026-10-24); `latest_metrics[] {indicator, value, unit, method_code, method_version, measured_at, disclaimer_key}` con el último resultado por indicador **dentro del periodo** (decisión del 2026-10-24); `sources[]` con el estado (`OK`/`UNAVAILABLE`) de las consultas `training_sessions` y `training_metrics`, como HU035 (decisión del 2026-10-24). Si falla una consulta, esa fuente sale `UNAVAILABLE` y el tablero conserva la otra; si fallan ambas, `503 upstream_unavailable`. Minutos enteros `ROUND_HALF_UP` a partir de los segundos sumados. Sin datos: `has_data: false`, totales en cero y listas vacías. `Cache-Control: no-store`.
- **Distancia y Strava:** `training_session_history` todavía no tiene fuente ni distancia (llegan con HU031, no integrada): toda sesión cuenta como `SPORTAPP`, `STRAVA` va en cero y `distance_meters` es `null` cuando ninguna fila del periodo la tiene (decisión del 2026-10-24), nunca «0 m» inventado.
- **Consulta:** `GROUP BY` deporte y semana ISO en PostgreSQL (`date_trunc('week', <hora local>)`, la misma regla que `iso_week_start` del dominio, comprobada en la integración con `-05:00`, `+09:00` y UTC) sobre el índice `(athlete_id, ended_at, session_id)`, y `DISTINCT ON (metric_code)` sobre el índice `(athlete_id, measured_at DESC, calculation_id DESC)`. Sin migraciones ni vista materializada.
- **Rendimiento:** con 2.000 sesiones y 200 cálculos del mismo deportista en 366 días (más 2.000 sesiones de otros deportistas), el p95 del caso de uso completo midió entre 19 y 27 ms en local (prueba `test_p95_below_one_second_with_2000_sessions_and_200_calculations`, umbral 1 s): muy por debajo de los 500 ms a partir de los cuales ADR-043 pide evaluar una vista agregada.
- **Versiones:** `training-management` 0.10.0 y `bff-mobile` 0.9.0.

Detalles de HU034 (ADR-030, aceptada el 2026-10-12; decisiones de la tarea confirmadas el 2026-10-14):

- **Quién:** solo `athlete` con la capacidad `training.metrics.ftp_vo2max` vigente (el plan `FREE` del catálogo `plans-2026-10-11.v1` la incluye), verificada en `bff-mobile` y de nuevo en el servicio, siempre antes de abrir la transacción. El deportista sale del token: un `athlete_id` u otro campo extra en el cuerpo responde `422` con `field_not_allowed`. Otro actor → `403 permission_required`; sin la capacidad → `403 capability_required`; Afiliación sin respuesta → `503 upstream_unavailable` o `504 upstream_timeout`.
- **Catálogo:** `GET /v1/training-metrics/catalog` devuelve `catalog_version` y los dos métodos inmutables de ADR-030 con los metadatos para pintar el formulario (`metric_code`, `label_key`, `result_unit`, `result_scale`, `method_code`, `method_version`, `method_label_key`, `disclaimer_key` e `inputs[]` con `code`, `label_key`, `value_type: "INTEGER"`, `unit`, `minimum`, `maximum`, `scale` y `required`). Responde con `ETag: "<catalog_version>"`; si `If-None-Match` coincide, `304` sin cuerpo. El BFF reenvía la cabecera y devuelve `304` y `ETag` tal como llegan.
- **Métodos:** `FTP_20_MIN_95_V1` (`mean_power_watts` entero de 1 a 2.500 W; FTP estimado en W = potencia × 0,95, redondeo `ROUND_HALF_UP` a 0 decimales; 250 W → 238 W) y `COOPER_12_MIN_V1` (`distance_meters` entero de 600 a 6.000 m; VO₂max estimado en `mL/kg/min` = (d − 504,9) / 44,73, `ROUND_HALF_UP` a 1 decimal; 2.400 m → 42,4). Se calcula con `Decimal`, nunca con `float` ni con el valor del cliente. El catálogo es inyectable: una prueba registra un tercer método solo para la prueba (extensibilidad de HA014) y nada llega al producto.
- **Validación:** `422 validation_error` con `details[{field, issue}]`, rutas como `inputs[0].value`, `measured_at` o `method_version` y las causas de ADR-030 (`required`, `field_not_allowed`, `unknown_metric`, `unsupported_method_version`, `unknown_input`, `duplicate_input`, `invalid_unit`, `out_of_range`, `invalid_datetime`, `too_far_in_future`, `too_old`). Un valor no entero o no numérico responde `out_of_range`. `measured_at` solo se acepta como `AAAA-MM-DDTHH:MM:SSZ` (si no, `invalid_datetime`), hasta 5 minutos por delante del reloj y con no más de 365 días. Una entrada inválida no deja cálculo, clave ni evento. Dos causas no están en el catálogo de ADR-030 y quedan por ratificar: `invalid_uuid` (un `calculation_id` que no es UUID v4 o una `Idempotency-Key` que no es UUID) e `invalid_cursor` (cursor de la consulta inválido). En la consulta, `limit` fuera de 1–100 o no entero responde `query.limit: out_of_range` y un `metric_code` desconocido, `query.metric_code: unknown_metric`.
- **Respuesta:** `201` con el cuerpo enviado más `athlete_id`, `result {value, unit, scale}` (número JSON), `status: "CONFIRMED"`, `calculated_at` (UTC `Z`, lo asigna el backend), `disclaimer_key: "estimated_not_diagnostic"` y `version: 1`. El resultado siempre es una estimación de campo, no un diagnóstico.
- **Idempotencia:** `Idempotency-Key` UUID por deportista y operación (`calculate_training_metric`) durante 30 días, en la `command_idempotency` de HU032. Misma clave y payload → la respuesta original (`201`) con `Idempotent-Replayed: true`; otro payload → `409 idempotency_key_reused`. El mismo `calculation_id` con los mismos datos y otra clave → `200` con el resultado existente, sin otro evento; con datos distintos → `409 calculation_id_reused`.
- **Transacción y evento (CQRS, CMP-003):** cálculo, entradas normalizadas, clave e `indicador.calculado.v1` se confirman juntos (migración de escritura `0005_training_metrics`). El evento va a `sportapp.entrenamientos.v1` con `calculation_id` como `aggregate_id` y clave de orden, `aggregate_type` `training_metric_calculation`, versión 1 y la clave de idempotencia como `causation_id`; la respuesta no espera la proyección.
- **Proyección y consulta:** `entrenamientos-read-projector` guarda `training_metric_result` y `processed_event` en una transacción (migración de lectura `0006_training_metric_results`, índice `(athlete_id, measured_at DESC, calculation_id DESC)`); el mismo evento repetido se descarta y otra versión del mismo cálculo va a reintentos y DLQ. `GET /v1/training-metrics/calculations` lee solo los resultados del deportista del token, ordenados por `measured_at DESC, calculation_id DESC` con cursor keyset opaco, `limit` de 1 a 100 (20 por defecto) y `metric_code` opcional (desconocido → `unknown_metric`); sin resultados → `200` con `items: []`.
- **Auditoría:** `audit.training_metric_calculated` con `result` (`calculated`, `replayed`, `existing` o `rejected` con `reason`), `calculation_id`, `metric_code` y `athlete_id`; sin entradas, resultado, cuerpos ni token.
- **Identificadores:** un `calculation_id` ya usado por otro deportista responde `409 calculation_id_reused` (revela que existe, con riesgo bajo por ser UUID v4); no se valida que la `Idempotency-Key` sea distinta del `calculation_id`, aunque ADR-030 lo pide al cliente.
- **Versiones:** `training-management` 0.6.0 y `bff-mobile` 0.8.0 (la ficha reservaba 0.7.0, que ya tomó HU041).
- **Rendimiento (HA014):** `load/locust/training_metrics.py` reproduce la mezcla de ADR-030 (30 % catálogo, 35 % cálculo y 35 % consulta) contra `bff-mobile` a tasa constante (por defecto 30 rps durante 10 minutos, después de 60 s de calentamiento que no cuentan) y termina con código 1 si alguna operación llega a p95 de 3 s o a 1 % de error, o si no sostiene el 90 % de la tasa (los rechazos funcionales esperados se informan aparte). Los parámetros y el dataset están en la cabecera del script. Ejemplo con cuentas preparadas (CSV `email,password` de deportistas con plan):

  ```text
  HA014_ATHLETES_FILE=deportistas.csv uv run --group load locust -f load/locust/training_metrics.py --host http://127.0.0.1:8000 --headless --csv reports/load/ha014 --only-summary
  ```

  Sin `HA014_ATHLETES_FILE`, cada usuario virtual registra un deportista sintético en `HA014_REGISTRATION_URL` y espera a que el alta gratuita active la capacidad. La corrida completa queda **BLOQUEADA** hasta tener un ambiente representativo.

Detalles de HU024 (ADR-033, aceptada el 2026-10-12; decisiones de la tarea del 2026-10-18):

- **Publicar:** solo `organizer` con `events.publish` (BFF y servicio) y propietario del evento; `organizer_id` sale del token. Inexistente o ajeno → `404 event_not_found`. Con la fila de `event` bloqueada (`FOR UPDATE`): `DRAFT` → `PUBLISHED`, versión + 1, `updated_at` = `published_at` = reloj del servidor; evento, clave (`command_idempotency`, `operation = publish_event`, 24 h) y `evento.publicado.v1` en un COMMIT. La respuesta no espera la proyección.
- **Requisitos (CA2):** el agregado persistido se revalida con todas las reglas de ADR-028 (el inicio se expresa en su zona declarada) y `starts_at` debe ser posterior a la publicación; si no, `422 validation_error` con `details[{field, issue}]` y el evento sigue `DRAFT` sin datos parciales. La tarifa no es requisito.
- **Idempotencia (CA4):** huella organizador + operación + `event_id`. Misma clave → la respuesta original con `Idempotent-Replayed: true`; misma clave con otro evento → `409 idempotency_key_reused`; ya publicado con otra clave → `200` con el estado vigente, sin versión ni evento (esa clave también queda registrada). Ocho publicaciones simultáneas producen una transición y un evento.
- **Eventos propios:** `GET /v1/me/events` para cualquier `organizer` (decisión del responsable: HU018 y HU022 la reutilizarán); `status` opcional (`DRAFT`/`PUBLISHED`; sin él, ambos), `limit` 1–100 (20) y cursor keyset `starts_at, event_id`. Cada elemento es el agregado completo de la respuesta de publicar (`published_at` `null` en borrador). Lee `event_read`.
- **Detalle:** el mismo esquema de HU041 con `status` `DRAFT|PUBLISHED` (decisión del responsable); un `DRAFT` solo lo recibe su organizador; `bff-web` lo entrega sin `my_registration`.
- **Proyección (ADR-033, CMP-004):** `eventos-read-projector` aplica `evento.creado.v1` y `evento.publicado.v1` por estado completo: fila ausente → se inserta (aunque llegue primero la versión 2); versión mayor → sustituye todas las columnas; menor o igual → se confirma sin efectos. Sustituye la regla «solo la siguiente versión» de HU021. Si dos mensajes del mismo evento crean la vista a la vez, el que pierde se reaplica por versión.
- **Migraciones:** escritura `0004_event_publication` (`event.published_at` y la restricción `(status = 'PUBLISHED') = (published_at IS NOT NULL)`; un `PUBLISHED` sembrado antes recibe `published_at = updated_at`, decisión del responsable); lectura `0005_event_read_publication` (`event_read.published_at`). ADR-033 las sugería como `0003`/`0004`, que ya tomó HU041.
- **Auditoría:** `audit.event_published` con resultado (`published`, `already_published`, `replayed`, `rejected` con `reason`), `event_id`, `organizer_id` y correlación; sin textos libres, ubicación ni token.

Detalles de HU022 (ADR-039, aceptada el 2026-10-12; tarea del 2026-10-24):

- **Quién:** solo `organizer` con el permiso por actor `events.attendees.view`, filtrado en `bff-web` y de nuevo en el servicio (`403 permission_required`); el evento debe ser propio: uno ajeno o inexistente es `404 event_not_found`, sin revelar participantes (CA2).
- **Eventos (`events-management` 0.7.0):** lee `event_registration_read` (HU041) con `registration_id`, `athlete_id`, `status`, `registered_at` y `applied_fee`, en orden `registered_at`, `registration_id` ascendente con cursor keyset opaco; `limit` 1–100. `422 validation_error` en `query.status` (`unknown_value`), `query.limit` (`out_of_range`) o `query.cursor` (`invalid_cursor`). Propiedad, conteos y página se leen en una sola transacción `REPEATABLE READ`. Migración de lectura `0006_registration_event_order`: índice `(event_id, registered_at, registration_id)` (HU041 solo creó el índice por deportista). Auditoría `audit.attendees_access` con organizador, evento, resultado y cantidad devuelta, sin datos de los inscritos.
- **Usuarios (`users-management` 0.11.0):** contrato interno nuevo `POST /v1/users/directory-lookup` (ADR-039) `{user_ids}` → `{items:[{user_id, full_name}]}` desde `user_directory`, sin correo, perfil ni datos de salud. No reutiliza `POST /v1/users/lookup-batch` (HU011), que también entrega el correo. Misma validación que `lookup-batch` (1–100 UUID únicos; `too_short`, `too_long`, `duplicate_value`, `uuid_parsing`); las cuentas inexistentes, deshabilitadas o sin nombre proyectado se omiten. Auditoría `audit.directory_lookup` con el número pedido y encontrado, sin nombres ni identificadores.
- **`bff-web` 0.22.0:** reenvía la consulta a Eventos y compone `display_name` con un solo lote de `athlete_id` únicos de la página; sin inscritos no consulta Usuarios. Si Usuarios no responde a tiempo o falla, la lista se entrega con `display_name: null` y `sources: [{source: "users", status: "UNAVAILABLE"}]` (CA6). `no-store`. No expone el contrato interno.
- **Por ratificar** (el paquete y ADR-039 no los fijan; se implementaron con el precedente más cercano):
  - `status` por defecto `REGISTERED` (como «mis inscripciones» de HU041) y `ALL` sin filtro;
  - `limit` por defecto 20 (como HU041 y HU024);
  - `total` = inscripciones que cumplen el filtro de `status`; `by_status` = todas las del evento por estado;
  - `sources[]` solo con la fuente `users`;
  - `directory-lookup` con la misma validación y semántica de omisión que `lookup-batch`, sin token y sin rechazo de `Authorization`;
  - nombres de auditoría `audit.attendees_access` (paquete) y `audit.directory_lookup`.

Detalles de HU023 (ADR-040, aceptada el 2026-10-13; decisiones de la tarea del 2026-10-20):

- **Quién:** solo `organizer` con `events.calendar.view` (`contracts/authorization/actor-permissions.v1.json`, canal web), filtrado en `bff-web` y de nuevo en `events-management` (`403 permission_required`). El organizador no consulta Afiliación; el deportista conserva la capacidad `calendar.sport.view` de HU035 y cualquier otro actor recibe `403`.
- **Calendario (CA1, CA4):** `GET /v1/events` lee `event_read` (solo `PUBLISHED`, sin `organizer_id`) con superposición del intervalo del evento con `[from, to]`. Los filtros opcionales `discipline` y `city` comparan por igualdad tras recortar, pasar a minúsculas y quitar los acentos (`domain/sport_events/terms.py`: una tabla de equivalencias Unicode que la consulta SQL aplica con `lower(translate(...))`, sin migración). Términos de 2 a 80 caracteres (`invalid_length`). Orden y cursor keyset `(starts_at, event_id)`; el cursor se reenvía con los mismos filtros. `bff-web` reenvía parámetros, cuerpo y errores sin alterar, con `Cache-Control: no-store`.
- **Periodo (CA3):** como máximo 92 días (`query.to` `out_of_range`) para cualquier actor. `to` anterior a `from` es `ends_before_starts`; para el organizador también `from == to` (decisión del responsable del 2026-10-20: ADR-040 rige para HU023 y el deportista conserva `from ≤ to` de ADR-031; `bff-mobile` ya limitaba la ventana a 92 días).
- **Coincidencias (CA2):** `GET /v1/me/events/{event_id}/conflicts` solo en `bff-web`. El evento propio (`DRAFT` o `PUBLISHED`) se busca en `GET /v1/me/events` página a página (límite 100; decisión del responsable del 2026-10-20, porque el detalle público no prueba la propiedad); ajeno o inexistente → `404 event_not_found`. Luego se consultan todas las páginas de `GET /v1/events` en el periodo del evento, una vez con `discipline` y otra con `city`, para que la normalización sea la del servicio. Solo coinciden los solapamientos estrictos (`ends_at == starts_at` no coincide). El propio evento se excluye, los motivos se unen como `SAME_DISCIPLINE` y `SAME_CITY` (en ese orden) y el orden es `starts_at`, `event_id`. Respuesta `{conflicts[{event_id, name, discipline, city, starts_at, ends_at, time_zone, reasons}], evaluated_at}` con `no-store`. Es información de apoyo: no bloquea publicar ni reserva fechas.
- **Fallas:** cada llamada usa el timeout del cliente de Eventos (`UPSTREAM_TIMEOUT_SECONDS`); sin respuesta → `504 upstream_timeout`; caído o con un estado inesperado → `503 upstream_unavailable`; el `403` del servicio se reenvía.
- **Pruebas:** unitarias de normalización, filtros, permisos, periodo y keyset en el servicio; integración con PostgreSQL (150 eventos con empates y borradores, filtros combinados y valores con acentos o mayúsculas guardados); unitarias del BFF (filtro de borde, reenvío, motivos, bordes, evento propio en otra página, `404`, paginación de las fuentes, `503`/`504`); contrato BFF ↔ servicio; componente BFF → Eventos real con Pub/Sub (`bff/web/tests/component/test_event_calendar_flow.py`).

Detalles de HU041 (ADR-032, aceptada el 2026-10-12):

- **Quién:** solo `athlete`, filtrado en `bff-mobile` y de nuevo en el servicio (`403 permission_required`); el deportista sale del token y el cliente nunca envía `athlete_id`. No exige capacidad de plan.
- **Elegibilidad y errores:** evento `PUBLISHED` con `starts_at` futuro; inexistente o `DRAFT` → `404 event_not_found`; ya iniciado → `409 event_registration_closed`; ya inscrito → `409 event_registration_exists` con `details=[{"field": "registration_id", "issue": "<uuid>"}]` para que la app abra la inscripción existente (el formato de `details` es elección de esta tarea); sin cupo → `409 event_full`; `Idempotency-Key` ausente o no UUID → `422 validation_error`.
- **Cupo y unicidad:** el comando bloquea la fila de `event` (`FOR UPDATE`), cuenta las inscripciones frente a `capacity` y guarda inscripción, clave y `usuario.inscrito.v1` en la misma transacción; la unicidad `(event_id, athlete_id)` de la base respalda la regla. Ocho solicitudes concurrentes al último cupo → una `201` y siete `event_full` (prueba de integración).
- **Idempotencia:** clave por deportista y operación, 24 h. `command_idempotency` pasa a tener PK `(actor_id, operation, key)` (migración de escritura `0003_event_registration`), así que la misma clave en HU021 y en HU041 no se cruza. Misma clave y evento → respuesta original con `Idempotent-Replayed: true` (también si llegan a la vez); otro evento → `409 idempotency_key_reused`.
- **Estado y tarifa:** `REGISTERED`, versión `1`; `applied_fee` es informativa y hoy `null` (las tarifas son de HU025); sin cobro ni estados de pago. `201` con `Location: /v1/me/event-registrations/{registration_id}`.
- **Proyección:** `eventos-read-projector` despacha por tipo (`evento.creado.v1`, `evento.publicado.v1` y `usuario.inscrito.v1`); la inscripción se guarda en `event_registration_read` (migración de lectura `0004_event_registration_read`, porque `0003` ya la usó HU035) junto con `processed_event`; la misma versión repetida se descarta y otra versión del mismo `registration_id` va a reintentos y DLQ.
- **Consultas:** el detalle lee `event_read` (solo `PUBLISHED`; `fee` en `null` hasta HU025); en el servicio es visible para cualquier actor autenticado, como fija ADR-033 (desde HU024 el `DRAFT` también lo ve su organizador), y `my_registration` solo se compone para el deportista desde `event_registration_read` (en `bff-mobile` solo entra `athlete`); «mis inscripciones» filtra por el deportista del token y `status` (`REGISTERED`), `limit` 1-100 (20 por defecto) y cursor keyset `starts_at, registration_id`. Ambas leen la proyección, así que una inscripción recién creada aparece con el retraso del proyector.
- **Modelo de escritura:** `event.status` admite `PUBLISHED` (restricción ampliada) para que la inscripción pueda validarlo; la transición `DRAFT → PUBLISHED` la hace HU024. Las pruebas de HU041 siembran eventos publicados de forma sintética (con `published_at`).
- **Auditoría:** `audit.event_registration_created` con resultado, `registration_id`, `event_id`, `athlete_id` y correlación; sin cuerpos ni token.

Detalles de HU035 (ADR-031, aceptada el 2026-10-12):

- **Quién:** solo `athlete` con `calendar.sport.view` vigente (Afiliación), verificado en `bff-mobile`; `events-management` vuelve a verificar la capacidad antes de servir eventos (`403 capability_required`, `503 upstream_unavailable` si no puede evaluarla, también ante timeout); `training-management` solo exige `athlete` y usa el `athlete_id` del token, sin capacidad, para las sesiones propias.
- **Periodo:** `from`/`to` instantes ISO 8601 con desfase (zona del dispositivo); sin ambos, el mes UTC en curso (`to` = último segundo del mes); `from` ≤ `to`; hasta 92 días. Errores `422 validation_error` con `invalid_datetime`, `ends_before_starts`, `out_of_range` y, si llega solo uno de los dos, `required` (ADR-031 no fija ese caso). Las sesiones se piden por las fechas de `from` y `to` en el desfase recibido.
- **Fuentes:** `EVENT_PUBLISHED`, eventos `PUBLISHED` de `event_read` que se superponen con el periodo (bordes incluidos; nunca borradores ni `organizer_id`); `TRAINING_PLAN_SESSION`, sesiones de planes `ASSIGNED` del deportista con fecha en el periodo (inclusiva). Hoy no existen eventos `PUBLISHED`: la publicación es de HU024.
- **Sesiones planificadas:** `plan.entrenamiento_creado.v1` no lleva identificador por sesión, así que la vista `training_plan_session_view` (migración de lectura `0005`, con backfill desde `training_plan_view`) usa un UUIDv5 determinista de `plan_id` y la posición de la sesión, el mismo que calcula el proyector; reconstruir la vista da los mismos identificadores.
- **Composición:** consultas en paralelo con el timeout de cada cliente; hasta 200 elementos por fuente con `truncated` (no se pagina la composición). Una fuente caída → `200` con la otra y `sources[{name, status: OK|UNAVAILABLE|TIMEOUT, truncated}]`; ambas → `504 upstream_timeout` si las dos vencieron el tiempo, si no `503 upstream_unavailable`. Orden `occurs_on`, `starts_at` (nulos primero), `source`, `item_id`; `occurs_on` es la fecha del evento en su `time_zone` o la `scheduled_date`.
- **Tiempo:** instantes en UTC `Z` y `time_zone` del evento; las sesiones solo tienen fecha. El móvil convierte a la zona del dispositivo.
- **Observabilidad:** `bff.calendar` registra estado y número de elementos por fuente y la latencia, sin cuerpos ni identificadores; la correlación se propaga a ambas consultas paralelas.

Detalles de HU032 (ADR-029, aceptada el 2026-10-12):

- **Quién:** solo `athlete`, filtrado en `bff-mobile` y de nuevo en el servicio. Iniciar exige `training.session.record` en Afiliación, consultada **antes** de abrir la transacción; finalizar o consultar la sesión propia no vuelve a pedirla. Sin respuesta de Afiliación: `503 upstream_unavailable`, o `504 upstream_timeout` si venció el tiempo (HU010 sigue respondiendo `503` en ambos casos).
- **Inicio:** `session_id` UUID v4 generado por el móvil, `sport` del catálogo de HU010, `plan_id` opcional (plan propio `ASSIGNED` que incluya ese deporte; si no, `404 training_plan_not_found`) y `started_at`. Responde `201` con `Location` y la sesión `ACTIVE`, versión 1. Una restricción única parcial de la base (`uq_training_session_athlete_active`) permite una sola sesión activa por deportista: la carrera responde `409 active_session_exists`.
- **Fin:** `POST /v1/training-sessions/{session_id}/finish` con `ended_at`; bloquea la sesión, calcula `duration_seconds` (diferencia entera, puede ser 0) y responde `200` con `FINISHED`, versión 2. Sesión inexistente o ajena: `404 training_session_not_found`. Repetirlo con el mismo `ended_at` devuelve la sesión terminada; con otro, `409 training_session_already_finished`. `training_session_not_active` no se emite: no existen más estados que `ACTIVE` y `FINISHED`.
- **Tiempo:** `started_at` y `ended_at` son la acción observada por el móvil, en UTC `YYYY-MM-DDTHH:MM:SSZ`; se rechazan con más de 5 minutos hacia el futuro (`too_far_in_future`) o más de 30 días de antigüedad (`too_old`); `ended_at` anterior al inicio → `ends_before_starts`. La duración enviada por el cliente se rechaza (`field_not_allowed`).
- **Idempotencia:** `Idempotency-Key` UUID distinta por comando, aislada por deportista y operación durante 30 días (`command_idempotency`; la tabla `idempotency_key` de HU010 no cambia). Misma clave y payload → resultado original con `Idempotent-Replayed: true`; otro payload → `409 idempotency_key_reused`. El mismo `session_id` con el mismo inicio devuelve la sesión vigente sin otro evento; con otros datos o de otra persona → `409 session_id_reused`.
- **Eventos (CQRS, CMP-003):** sesión, clave y evento se confirman juntos. `entrenamiento.iniciado.v1` (versión 1) y el `entrenamiento.finalizado.v1` ya aprobado para HU011 (versión 2, esquema sin cambios), ambos con `session_id` como clave de orden y la clave de idempotencia como `causation_id`. Con HU032 el historial de HU011 empieza a recibir datos reales.
- **Proyección:** `entrenamientos-read-projector` crea `active_training_session` con el inicio y, con el fin, la elimina y actualiza `training_session_history` en la misma transacción; si el fin llega antes que el inicio, el inicio tardío ya no crea una sesión activa. `GET /v1/training-sessions/active` lee esa vista (`200` o `204`) y nunca debe contradecir una versión local más reciente del móvil.
- **Errores de validación:** `422 validation_error` con `details[{field, issue}]` y `issue` en `required`, `field_not_allowed`, `unknown_value`, `invalid_uuid`, `invalid_datetime`, `too_far_in_future`, `too_old` o `ends_before_starts`. El BFF reenvía cuerpo, clave y correlación sin reinterpretarlos.
- **Auditoría:** `audit.training_session_started` y `audit.training_session_finished` con resultado, `session_id` y `athlete_id`; sin deporte, tiempos, cuerpos, token ni ubicación.

Detalles de HU021 (ADR-028, aceptada el 2026-10-12):

- **Quién crea:** solo `organizer` con `events.create`, verificado en el BFF y de nuevo en el servicio (que valida el access token con el JWKS de Usuarios, en caché, sin llamar a otro dominio durante el comando). `organizer_id` sale del token; en el cuerpo se rechaza (`field_not_allowed`), igual que `event_id`, `ends_at`, `status`, `version` y las marcas de tiempo.
- **Evento:** `name` (3–120), `description` (20–2.000) y `discipline` (2–80, texto libre sin catálogo) recortados; `location` con `name` (3–120), `address` (5–200), `city` (2–80) y `country_code` ISO 3166-1 alfa-2 en mayúsculas (lista de 249 códigos en el dominio); `starts_at` futuro con desfase explícito, coherente con la zona IANA `time_zone` en ese instante; `duration_minutes` de 15 a 1.440 y `capacity` de 1 a 100.000. El backend calcula `ends_at = starts_at + duration_minutes`.
- **Fechas:** `starts_at`, `ends_at`, `created_at` y `updated_at` se guardan y responden en UTC con sufijo `Z`; `time_zone` es un dato del evento (dónde ocurre). El frontend convierte a la zona del usuario.
- **Respuesta:** `201` con `Location: /v1/events/{event_id}` y el borrador `DRAFT`, versión 1 (contrato de ADR-028). La respuesta usa la escritura confirmada y no espera la proyección.
- **Idempotencia:** `Idempotency-Key` UUID por organizador, 24 horas (`command_idempotency`). Misma clave y payload normalizado → mismo `201` con `Idempotent-Replayed: true`; otro payload → `409 idempotency_key_reused`; la clave de otro organizador crea su propio evento. Solicitudes concurrentes con la misma clave producen un evento y un mensaje.
- **Transacción y evento (CQRS, CMP-004):** `event`, `command_idempotency` y `evento.creado.v1` en `outbox_event` se confirman juntos. `eventos-outbox-publisher` publica en `sportapp.eventos.v1` con `event_id` como clave de orden; `causation_id` es la clave de idempotencia. El payload lleva el estado completo del borrador, sin credenciales ni datos de pago.
- **Proyección:** `eventos-read-projector` consume `proyector-eventos-v1` (DLQ `sportapp.eventos.proyeccion.dlq`, 10 intentos, espera de 10 a 600 s: aprobados en ADR-028), deduplica por el `event_id` del sobre en `processed_event`, aplica solo la siguiente `aggregate_version` y confirma el mensaje después del COMMIT de `event_read`. Un `DRAFT` solo lo ve su organizador y nunca aparece en el catálogo público; HU021 no expone consultas HTTP (las agregan HU022-HU024).
- **Errores de validación:** `422 validation_error` con `details[{field, issue}]`; `field` usa rutas como `location.country_code` e `issue` es uno de `required`, `field_not_allowed`, `invalid_length`, `unknown_value`, `not_in_future`, `out_of_range`, `time_zone_unknown` o `time_zone_offset_mismatch`. El BFF reenvía cuerpo y clave sin reinterpretarlos.
- **Otros errores:** `401 invalid_access_token`, `403 permission_required`, `503 upstream_unavailable` (JWKS) y, en el BFF, `503`/`504 upstream_timeout`. ADR-028 nombra estos casos `unauthenticated`, `forbidden` y `service_unavailable`; se conservan los códigos que ya usa el repositorio (igual que en HU015).
- **Auditoría:** `audit.event_created` con resultado, `event_id`, `organizer_id` y `correlation_id`; sin textos libres, ubicación ni token.

Detalles de HU020 (ADR-038; decisiones aprobadas el 2026-10-14):

- **Quién:** solo `athlete` (`403 permission_required` en el BFF y en el servicio). La consulta no exige capacidad de plan salvo para la categoría médica.
- **Periodo y datos:** `from`/`to` obligatorios en instantes ISO 8601 con desfase, `from < to`, hasta 31 días. Se ofrecen los intervalos futuros que **empiezan** en `[from, to)` de los servicios `PUBLISHED` (los borradores nunca aparecen), con `starts_at`/`ends_at` en UTC `Z`, `time_zone` y `capacity`; tarifa `COP` como cadena con su unidad de cobro (informativa, no es un cobro ni un beneficio del plan). Sin parámetro de zona: el cliente convierte. La lectura no reserva ni bloquea cupos.
- **Filtros:** `category` y `modality` repetibles (`category=COACHING&category=TRANSPORT`, como pide ADR-041 para HU029); `availability=available` (por defecto) excluye los servicios sin intervalos en el rango y `all` los incluye con `available: false`.
- **Categoría médica:** `SPORTS_MEDICINE` solo con `services.sports_medicine.access`. El servicio consulta a Afiliación **solo si la categoría puede aparecer** (sin filtro de categoría o incluyéndola); sin la capacidad la omite y responde `restricted_categories: ["SPORTS_MEDICINE"]`, también si se pidió explícitamente (`200` vacío, no `403`). Si Afiliación no responde (error o timeout): `503 upstream_unavailable`; con categorías no médicas la consulta no depende de Afiliación. El BFF no consulta capacidades: la regla vive en el servicio.
- **Paginación:** orden por `name` y `service_id` con cursor keyset opaco; `limit` de 1 a 50 (20 por defecto).
- **Errores:** `422 validation_error` con `query.from`/`query.to` (`required`, `unknown_value`), `query.to` (`ends_before_starts`, `out_of_range` si supera 31 días), `query.category`, `query.modality`, `query.availability`, `query.cursor` (`unknown_value`) y `query.limit` (`unknown_value`, `out_of_range`), con el catálogo de ADR-027.
- **Persistencia:** migración `0004_service_availability_read` con los índices `ix_service_status_category` y `ix_service_availability_service_id_starts_at`; sin CQRS ni eventos.

Detalles de HU019 (ADR-027 «Contrato de publicación de HU019» y ADR-038; decisiones aprobadas el 2026-10-13):

- **Quién publica:** solo el `service_provider` propietario con `services.publish`, verificado en el BFF y de nuevo en el servicio; `provider_id` sale del token. Un servicio inexistente o ajeno responde `404 service_not_found` (no se revela que existe).
- **Transición:** `DRAFT` → `PUBLISHED`, una sola vez: sube la versión (de 1 a 2 en un borrador de HU015) y fija `published_at`. Si ya está publicado, otra clave devuelve `200` con el estado vigente, sin subir versión ni emitir otro evento. `200` con `{service_id, provider_id, status, version, published_at, updated_at}` en UTC `Z`.
- **Reglas:** la oferta almacenada se valida de nuevo con las reglas de HU015 y necesita al menos un intervalo con inicio futuro; los vencidos se ignoran. Sin ninguno futuro: `422 validation_error` con `{field: "availability", issue: "required"}`.
- **Idempotencia y concurrencia:** `Idempotency-Key` UUID por profesional, 24 horas (la misma tabla de HU015, PK `provider_id, key`). Misma clave y servicio → respuesta original con `Idempotent-Replayed: true`; la clave usada para otro servicio (o al crear) → `409 idempotency_key_reused`. El comando bloquea el servicio (`FOR UPDATE`) y confirma servicio, clave y outbox en una transacción: dos publicaciones simultáneas producen una transición, una versión y un evento.
- **Evento:** `servicio.publicado.v1` en `sportapp.servicios.v1`, clave de orden `service_id`, `aggregate_version` = versión nueva, `causation_id` = `Idempotency-Key`; payload mínimo `{service_id, provider_id, status, published_at}` (sin nombre, tarifa ni disponibilidad). Base única, sin CQRS: no hay proyector, base de lectura ni suscripción consumidora; en el emulador solo se crea el tópico (`python -m complementary_services.workers.pubsub_setup`).
- **Publicador (`servicios-outbox-publisher`):** `python -m complementary_services.workers.outbox_publisher`. Reclama con arrendamiento y `SKIP LOCKED`, publica fuera de transacción y marca `published_at` solo tras el ACK. Un fallo reprograma con espera de 10·2^(n−1) s (tope 600 s, jitter de ±10 % sin salir de 10–600 s); el evento nunca se descarta y desde el décimo fallo se registra `outbox.publish_alert` en nivel `ERROR` con `event_id` y `attempts` (antes, `outbox.publish_failed` en `WARNING`). Los publicados se conservan siete días: la purga corre como máximo una vez por hora por réplica y nunca toca pendientes.
- **Consultas propias (ADR-038):** `GET /v1/me/services` filtra por `status` (`DRAFT`|`PUBLISHED`), ordena por `updated_at` descendente y `service_id`, con cursor keyset y `limit` de 1 a 100 (20 por defecto); `GET /v1/me/services/{service_id}` devuelve el servicio completo o `404 service_not_found`. Otro actor → `403 permission_required`. El servicio completo incluye ahora `published_at` (`null` en borrador), también en la respuesta de `POST /v1/services`.
- **Otros errores:** `401 invalid_access_token`, `403 permission_required`, `503 upstream_unavailable` y, en el BFF, `504 upstream_timeout`. ADR-027 nombra estos casos `unauthenticated`, `forbidden` y `service_unavailable`; se conservan los códigos del repositorio (pendiente de alinear en la documentación). Un `service_id` de la ruta que no es UUID responde `422` con `{field: "path.service_id", issue: "uuid_parsing"}` del manejador común, antes de autenticar, en el servicio y en el BFF (convención del repositorio, como `event_id` en HU041); ADR-027 llamaría `unknown_value` a ese caso.

Detalles de HU015 (ADR-027, aceptada el 2026-10-12):

- **Quién crea:** solo `service_provider` con `services.create`; `provider_id` sale del token y, si viene en el cuerpo, se rechaza (`field_not_allowed`). El servicio nace `DRAFT`, versión 1, y no aparece en ningún catálogo hasta publicarse (HU019).
- **Oferta:** nombre de 3 a 120 caracteres y descripción de 20 a 2.000, tras recortar espacios exteriores; categoría `SPORTS_MEDICINE`, `COACHING`, `NUTRITION`, `TRANSPORT` o `ACCOMPANIMENT`; modalidad `IN_PERSON`, `VIRTUAL` o `HYBRID`; tarifa `{amount, currency, billing_unit}` con `amount` como **cadena** decimal de `0.00` a `999999999.99` (máximo dos decimales), `currency = COP` y unidad `SERVICE`, `SESSION`, `HOUR` o `DAY`. Es un precio público de referencia con impuestos incluidos: no crea cobro, factura ni pago, y es independiente del plan de afiliación.
- **Disponibilidad inicial (opcional):** hasta 100 intervalos explícitos `[starts_at, ends_at)` con desfase UTC, zona IANA (`time_zone`) y capacidad de 1 a 100; inicio futuro, duración de hasta 24 horas y fin dentro de 365 días; el desfase debe coincidir con la zona en cada instante; contiguos permitidos, superpuestos rechazados (se compara en UTC).
- **Fechas:** el backend guarda y responde los instantes en UTC con sufijo `Z`; `time_zone` es un dato de la oferta (dónde ocurre el servicio), no una conversión. El frontend convierte a la zona del usuario.
- **Idempotencia:** `Idempotency-Key` UUID por profesional, 24 horas. Misma clave y payload normalizado → mismo `201` con `Idempotent-Replayed: true`; otro payload → `409 idempotency_key_reused`; la misma clave de otro profesional crea su propio servicio.
- **Errores de validación (ADR-027 §14):** `422 validation_error` con `details[{field, issue}]`; `field` usa índices entre corchetes (`availability[0].time_zone`) y `issue` es uno de `required`, `field_not_allowed`, `unsupported_currency`, `out_of_range`, `too_many_decimals`, `invalid_length`, `unknown_value`, `not_in_future`, `ends_before_starts`, `overlapping_interval`, `time_zone_unknown`, `time_zone_offset_mismatch` o `too_many_intervals`. Por eso esta ruta lee el JSON sin la validación automática de FastAPI; el esquema se publica igualmente en el OpenAPI. El BFF reenvía cuerpo y clave sin reinterpretarlos.
- **Otros errores:** `401 invalid_access_token`, `403 permission_required`, `503 upstream_unavailable` (JWKS) y, en el BFF, `504 upstream_timeout`. Sin eventos (`servicio.creado` no existe; `servicio.publicado.v1` es de HU019).

Detalles de HU011 (decisiones y contratos aprobados el 2026-10-12):

- **Quién consulta:** el deportista, su propio historial (sin capacidad de plan: ADR-024 no define una para HU011); el `service_provider` con `training.history.view_assigned`, el de un deportista con autorización vigente (la de HU010). Otro actor o la ruta equivocada → `403 permission_required`; sin autorización vigente → `403 athlete_authorization_required`. El acceso del profesional se audita (`audit.training_history_access`).
- **Autorización proyectada:** cada cambio real de la autorización (otorgar, revocar, reactivar) deja en la outbox, en la misma transacción, `autorizacion_entrenamiento.otorgada.v1` o `.revocada.v1` con el estado completo; `aggregate_id` es un UUIDv5 determinista de la pareja y la versión es la de la autorización. El proyector aplica un evento solo si su versión es mayor que la proyectada. Una revocación surte efecto en la consulta cuando el proyector la aplica. La migración de escritura `0003_authorization_backfill` publica un evento por cada autorización creada antes de HU011.
- **Datos:** `entrenamiento.finalizado.v1` (`session_id`, `athlete_id`, `plan_id` opcional, `sport`, `started_at`, `ended_at`, `duration_seconds`, `status`), sin métricas de salud ni ubicación. Lo produce HU032 al finalizar una sesión (versión 2 del agregado `training_session`).
- **Filtros y orden (CA1, CA2):** `from` y `to` son fechas de fin inclusivas en UTC (`to` ≥ `from`); `sport` del catálogo de HU010. Orden `ended_at` descendente y, en empate, `session_id` descendente. Sin historial: `items: []` y `filters.sports` con los deportes disponibles.
- **Paginación (CA4):** `limit` de 1 a 100 (20 por defecto) y `next_cursor` opaco; el cursor marca la última sesión entregada (keyset), así que las páginas no pierden ni repiten registros aunque lleguen sesiones nuevas. Cursor, rango o límite inválidos → `422 validation_error` con el parámetro en `details`.
- **Selección directa del profesional (CA5, decisión del 2026-10-18):** `bff-web` 0.15.0 `GET /v1/me/training-athletes?limit&cursor` (solo `service_provider` con `training.history.view_assigned`) devuelve `{items:[{athlete_id, full_name, email, authorized_at}], next_cursor}`. Toma la página de `training-management` 0.8.0 `GET /v1/me/authorized-athletes` (autorizaciones `ACTIVE` del profesional del token en `training_authorization_view`, orden `authorized_at` y `athlete_id` descendentes, índice `ix_training_authorization_view_professional_order` de la migración de lectura `0007_authorized_athletes_index`) y agrega nombre y correo con el contrato interno `users-management` 0.7.0 `POST /v1/users/lookup-batch` (1 a 100 UUID únicos; devuelve las cuentas activas con nombre proyectado, en el orden pedido; `422 validation_error` con `too_short`, `too_long`, `uuid_parsing` o `duplicate_value`). Los `user_ids` salen solo de la página de Entrenamientos; si Usuarios no devuelve a alguno, cae o rechaza → `503 upstream_unavailable` (`504 upstream_timeout` por tiempo) y nunca un UUID sin nombre. Página vacía → no se llama a Usuarios. Nombre y correo no se guardan en Entrenamientos ni van a logs o URL (`Cache-Control: no-store`). Una revocación retira al deportista cuando el proyector la aplica.
- **Nombre en el directorio:** la migración de lectura `0004_user_directory_full_name` agrega `user_directory.full_name` (nulo) y `usuarios-read-projector` lo guarda desde `usuario.registrado.v1`. Las filas proyectadas antes no tienen nombre: el Job `users-management-directory-backfill` (`python -m users_management.workers.directory_backfill`, después de `users-management-migrate-read`) las completa una vez desde `user_account`. Es idempotente y registra solo conteos. Mientras una fila no tenga nombre, el lote la omite y el BFF responde `503`.

Detalles de HU014 (ADR-037, aceptada el 2026-10-12; decisiones de la tarea del 2026-10-22):

- **Quién:** crea solo el `service_provider` con `nutrition.plan.author` (BFF y servicio) y autorización `ACTIVE` del deportista (la de HU010); el BFF resuelve el correo con `POST /v1/users/lookup` (inexistente o que no es deportista → `403 athlete_authorization_required`). Consulta el creador con permiso y autorización vigente o el deportista dueño (el BFF ya filtra en el borde a quien no es deportista ni autor); un tercero recibe `404 nutrition_plan_not_found`. `GET /v1/me/nutrition-plans` es solo del deportista y no exige capacidad de plan (opción base «Mi alimentación»).
- **Cuerpo y reglas:** `title` 1–120, `objectives` 1–5 × 1–200, `start_date` ≤ `end_date` con un periodo de hasta 26 semanas, `meals[]` 1–8 (menú diario del periodo; `meal_type` `BREAKFAST`, `LUNCH`, `DINNER`, `SNACK` o `HYDRATION`) con `items[]` 1–15 `{food_code, quantity, unit}`. `quantity` es una cadena decimal entre `0.1` y `5000.0` con un decimal (se normaliza: `"150"` → `"150.0"`); `unit` debe estar permitida para el alimento. Las reglas fallan con `422 nutrition_plan_invalid` y `details[{field, issue}]` (rutas como `body.meals.0.items.1.food_code`): `end_date_before_start_date`, `period_too_long`, `unknown_value`, `unit_not_allowed`, `out_of_range`, `conflicts_with_allergy`, `conflicts_with_restriction`. La forma del cuerpo es `422 validation_error`.
- **Catálogo de alimentos:** `food-catalog-2026-10-13.v1`, cerrado, precargado por la migración `0006_nutrition_plans` (41 alimentos de las nueve categorías de ADR-037, propuesta del agente aprobada en el plan del 2026-10-22 y pendiente de ratificar en la ficha). Cada alimento lleva `allergens[]` (catálogo de alergias de ADR-035) y `incompatible_with[]` (patrones de dieta y restricciones de ADR-035), para que el bloqueo sea solo de datos. Ampliarlo es otra migración. `GET /v1/food-catalog` responde `ETag` `"food-catalog-2026-10-13.v1"` y `304` con `If-None-Match` (CORS de `bff-web` admite `If-None-Match` y expone `ETag`); la readiness falla si el catálogo no está completo en esa versión.
- **Restricciones (ADR-035):** antes de la transacción, `training-management` consulta `GET /v1/users/{user_id}/nutrition-restrictions` (interno, sin token). Alimento con un alérgeno del deportista → `conflicts_with_allergy`; alimento incompatible con su `diet_pattern` o sus `restrictions` → `conflicts_with_restriction`; **bloquean**, no advierten. Sin hábitos (`version: null`) → el plan se crea con `warnings: ["nutrition_habits_missing"]`. Usuarios caído, sin respuesta o con un estado inesperado → `503 upstream_unavailable` sin plan. Las restricciones no se guardan ni se registran. La autorización se comprueba antes de consultar Usuarios, para que un profesional no autorizado nunca provoque esa lectura.
- **Usuarios:** el contrato interno responde «sin hábitos» (`200 {restrictions: [], allergies: [], diet_pattern: null, version: null}`) a toda cuenta activa del directorio que no registró hábitos alimenticios; inexistente o deshabilitada → `404 user_not_found`. Desde HU009 devuelve los hábitos proyectados, así que el bloqueo por alergia o restricción ocurre de verdad (prueba de componente `test_registered_habits_block_the_plan_through_the_real_contract`). Cada lectura se audita (`audit.nutrition_restrictions_read`) sin valores.
- **Comando:** idempotencia en `idempotency_key` (24 h; huella con el profesional, el deportista y el cuerpo normalizado; misma clave con otro cuerpo → `409 idempotency_key_reused`; el reintento devuelve el mismo `warnings` sin volver a consultar Usuarios). Una transacción guarda la autorización bloqueada (`FOR SHARE`), el plan `ASSIGNED` versión 1 (inmutable: sin edición ni versionado), comidas, ítems, la clave y `plan.alimentacion_creado.v1`. Auditoría `audit.nutrition_plan_created` (resultado, motivo, `blocked`, profesional, deportista y plan), nunca alimentos ni restricciones.
- **Evento y proyección:** `plan.alimentacion_creado.v1` (`aggregate_type = nutrition_plan`, `aggregate_id = ordering_key = plan_id`, estado completo sin `warnings`) → `entrenamientos-read-projector` → `nutrition_plan_view` (migración de lectura `0008_nutrition_plan_view`; inserta si falta, reemplaza si la versión es mayor; duplicados en `processed_event`). La consulta no espera la proyección.

Detalles de HU010 (decisiones y contrato aprobados el 2026-10-12):

- **Autorización previa del deportista:** el deportista (con la capacidad `training.plan.manage` de su plan) autoriza a un profesional por su correo; solo entonces ese profesional puede crearle y asignarle planes. Autorizar dos veces devuelve la existente (`200`); una revocada se reactiva (`201`). Revocar (`204`) solo exige ser deportista, nunca depende del plan contratado. La autorización vive en `training_management` (`training_authorization`).
- **Quién crea:** solo `service_provider`, con el permiso `training.plan.author` de `actor-permissions.v1`. BFF y servicio lo verifican; el servicio vuelve a validar el access token con el JWKS de Usuarios y consulta a Afiliación las capacidades del deportista.
- **Correos:** el BFF los resuelve con el contrato interno `POST /v1/users/lookup` (coincidencia exacta del correo normalizado; una cuenta deshabilitada no se resuelve) y envía al servicio solo identificadores. El lookup lee el directorio `user_directory` de la base de lectura de Usuarios, que mantiene `usuarios-read-projector` desde HU005 (refactor de HU001). Antes de HU005, el lookup respondía `404` para cualquier usuario real.
- **Sin enumeración al asignar:** un correo inexistente, de otro actor o sin autorización recibe el mismo `403 athlete_authorization_required`.
- **Reglas del plan (CA3):** título de 1 a 120 caracteres; de 1 a 5 objetivos de 1 a 200; `start_date` ≤ `end_date` y periodo máximo de 52 semanas; de 1 a 100 sesiones dentro del periodo, cada una con deporte (`running`, `cycling`, `swimming`, `strength`, `other`), duración de 1 a 600 minutos e intensidad RPE de 1 a 10 (escala CR10); como máximo 2 sesiones el mismo día. Las reglas cruzadas responden `422 training_plan_invalid` con cada regla incumplida en `details`; los rangos de cada campo, `422 validation_error`. Sin campo de notas libres.
- **Transacción e idempotencia:** plan, sesiones, `outbox_event` e `idempotency_key` se confirman juntos. La autorización se lee con bloqueo compartido: una revocación concurrente no se intercala con la creación. Un reintento con la misma clave durante 24 h devuelve el mismo `201` con `Idempotent-Replayed: true`; la clave de otro profesional nunca devuelve un plan ajeno (`409 idempotency_key_reused`).
- **Evento y lectura (CQRS, CMP-003):** `plan.entrenamiento_creado.v1` en `sportapp.entrenamientos.v1` con `plan_id` como clave de orden; lleva identificadores y el contenido estructurado del plan, sin datos de perfil. `entrenamientos-read-projector` consume `proyector-entrenamientos-v1` (DLQ `sportapp.entrenamientos.proyeccion.dlq`, 10 intentos, backoff 10–600 s: valores PROPUESTOS) y deduplica por `event_id` en `processed_event`; aplica solo la siguiente versión del plan.
- **Consulta (CA4):** `GET /v1/training-plans/{plan_id}` lee `training_plan_view`; solo la ven el profesional creador y el deportista asignado (con su capacidad). Para cualquier otro usuario, o mientras no se proyecta, responde `404 training_plan_not_found`. El comando responde sin esperar la proyección.
- **Errores:**

| Código | Estado | Origen |
| --- | --- | --- |
| `invalid_access_token` | 401 | BFF / servicio |
| `permission_required` | 403 | BFF / servicio (actor sin el permiso de la operación) |
| `capability_required` | 403 | servicio (deportista sin `training.plan.manage`) |
| `athlete_authorization_required` | 403 | BFF (correo no resoluble) / servicio (sin autorización activa) |
| `professional_not_found` | 404 | BFF (correo que no es de un profesional) |
| `training_plan_not_found` | 404 | servicio |
| `user_not_found` | 404 | Usuarios (lookup interno) |
| `idempotency_key_reused` | 409 | servicio |
| `validation_error` | 422 | BFF / servicio |
| `training_plan_invalid` | 422 | servicio |
| `upstream_unavailable` | 503 | BFF / servicio (JWKS o Afiliación no disponibles) |
| `upstream_timeout` | 504 | BFF |

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
| `HEALTH_DATA_CONSENT_VERSION` | users | Versión vigente del consentimiento de datos de salud que exigen `PUT /v1/me/physiological-profile` (HU005) y `PUT /v1/me/nutrition-habits` (HU009); obligatoria: sin ella el servicio no arranca. Local: `local-dev`; la política real sigue pendiente. |
| `JWT_PRIVATE_KEY` | users | Clave privada Ed25519 en PEM que firma los access tokens (obligatoria; un valor inválido detiene el arranque). En `.env` va en una sola línea con `\n` literales. En Kubernetes se lee del Secret `sportapp-users-jwt`. |
| `JWT_ISSUER`, `JWT_AUDIENCE` | users | `iss` y `aud` de los access tokens (por defecto `sportapp-users-management` y `sportapp`). |
| `ACCESS_TOKEN_TTL_SECONDS`, `REFRESH_TOKEN_TTL_SECONDS` | users | Vigencia del access token (900) y del refresh token (604800). |
| `LOGIN_MAX_FAILURES`, `LOGIN_FAILURE_WINDOW_SECONDS` | users | Control de abuso por correo + IP: fallos permitidos (5) y ventana (900 s). |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC` | publicador de usuarios | Proyecto y tópico (por defecto `sportapp.usuarios.v1`). |
| `PUBSUB_EMULATOR_HOST` | publicador de usuarios | Solo local/CI: dirige el cliente al emulador. |
| `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `WORKER_ID` | publicadores de usuarios y afiliación | Lote (100), arrendamiento (30 s) e identidad de la réplica (valores iniciales de CMP-001). |
| `AFFILIATION_EXPECTED_CATALOG_VERSION` | affiliation | Versión de catálogo que debe estar instalada para quedar listo (obligatoria; hoy `plans-2026-10-13.v1`, desde `0003_plan_price_offers`). Cambia en el mismo release que una migración de catálogo nueva. |
| `PUBSUB_PROJECT_ID`, `FREE_PLAN_SUBSCRIPTION`, `CONSUMER_MAX_MESSAGES` | consumidor del alta gratuita | Proyecto, suscripción (por defecto `sportapp.afiliacion.alta-gratuita.v1`) y mensajes simultáneos por réplica (10). |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC` | publicador de afiliación | Proyecto y tópico (por defecto `sportapp.afiliacion.v1`). |
| `USERS_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS` | bff-web, bff-mobile | URL del servicio de usuarios (obligatoria) y timeout (5 s). |
| `CORS_ALLOWED_ORIGINS` | bff-web | Orígenes del cliente web autorizados por CORS, separados por comas, con la forma `http(s)://host[:puerto]` y sin `/` final (por ejemplo `http://localhost:18082,http://localhost:4200`). Vacía o ausente: se rechaza toda llamada de otro origen. No admite comodines; un valor inválido detiene el arranque. Son también los únicos `Origin` aceptados para iniciar y renovar sesión (CSRF). |
| `REFRESH_COOKIE_SAME_SITE` | bff-web | `SameSite` de la cookie del refresh token: `strict` (por defecto), `lax` o `none`. Ver la nota de HU003 en [Estado](#estado). |
| `AFFILIATION_MANAGEMENT_BASE_URL` | bff-web, bff-mobile | URL del contrato interno de Afiliación para `GET /v1/me/capabilities` (obligatoria; en el clúster, `http://affiliation-management`). |
| `JWT_ISSUER`, `JWT_AUDIENCE` | bff-web, bff-mobile | `iss` y `aud` esperados en el access token (por defecto los mismos que emite `users-management`). |
| `ACTOR_PERMISSIONS_PATH` | bff-web, bff-mobile | Ruta del contrato de permisos por actor (`actor-permissions.v2.json` desde HU026; la v1 se conserva sin uso). La imagen la fija en `/opt/sportapp/contracts/authorization/`; en el repositorio se localiza sola. Un contrato ausente o inválido detiene el arranque. |
| `COMPLEMENTARY_SERVICES_BASE_URL` | bff-web | URL del servicio de servicios complementarios para HU015 (obligatoria; en el clúster, `http://complementary-services`). |
| `EVENTS_MANAGEMENT_BASE_URL` | bff-web, bff-mobile | URL del servicio de eventos masivos para HU021, HU023 y HU024 (web), HU035 y HU041 (móvil); obligatoria; en el clúster, `http://events-management`. |
| `USERS_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS`, `JWT_ISSUER`, `JWT_AUDIENCE`, `ACTOR_PERMISSIONS_PATH` | events | JWKS de Usuarios para verificar el access token (obligatoria), timeout (5 s), `iss`/`aud` esperados y contrato de permisos; además `DATABASE_WRITE_URL` y `DATABASE_READ_URL`. |
| `AFFILIATION_MANAGEMENT_BASE_URL` | events | Contrato interno de capacidades de Afiliación para `GET /v1/events` (HU035; obligatoria; en el clúster, `http://affiliation-management`). |
| `AFFILIATION_MANAGEMENT_BASE_URL` | complementary | Contrato interno de capacidades de Afiliación para la categoría médica de `GET /v1/services/availability` (HU020; obligatoria; en el clúster, `http://affiliation-management`). |
| `USERS_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS`, `JWT_ISSUER`, `JWT_AUDIENCE` | affiliation | JWKS de Usuarios para verificar el access token del deportista en HU016 (obligatoria desde 0.3.0; en el clúster, `http://users-management`), timeout (5 s) e `iss`/`aud` esperados. |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC`, `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `WORKER_ID` | publicador de eventos | Proyecto, tópico (por defecto `sportapp.eventos.v1`), lote (100), arrendamiento (30 s) e identidad de la réplica; usa `DATABASE_WRITE_URL`. |
| `PUBSUB_PROJECT_ID`, `PROJECTOR_SUBSCRIPTION`, `CONSUMER_MAX_MESSAGES` | proyector de eventos | Proyecto, suscripción (por defecto `proyector-eventos-v1`) y mensajes simultáneos por réplica (10); usa `DATABASE_READ_URL`. |
| `USERS_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS`, `JWT_ISSUER`, `JWT_AUDIENCE`, `ACTOR_PERMISSIONS_PATH` | complementary services | JWKS de Usuarios para verificar el access token (obligatoria), timeout (5 s), `iss`/`aud` esperados y contrato de permisos. |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC`, `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `OUTBOX_BUSY_POLL_SECONDS`, `OUTBOX_IDLE_POLL_MAX_SECONDS`, `WORKER_ID` | publicador de servicios complementarios | Proyecto, tópico (por defecto `sportapp.servicios.v1`), lote (100), arrendamiento (30 s), espera entre ciclos (0,25 s con trabajo, hasta 2 s sin él) e identidad de la réplica; usa `DATABASE_URL`. Reintentos (10–600 s), alerta (décimo fallo) y retención de publicados (7 días) son fijos de ADR-027. |
| `TRAINING_MANAGEMENT_BASE_URL` | bff-web, bff-mobile | URL del servicio de entrenamientos para HU010 (web), HU032 y HU034 (móvil); obligatoria; en el clúster, `http://training-management`. |
| `USERS_MANAGEMENT_BASE_URL`, `AFFILIATION_MANAGEMENT_BASE_URL`, `UPSTREAM_TIMEOUT_SECONDS` | training | JWKS de Usuarios para verificar el access token y contrato interno de restricciones alimenticias (HU014), contrato interno de capacidades de Afiliación (obligatorias), y timeout (5 s). |
| `JWT_ISSUER`, `JWT_AUDIENCE`, `ACTOR_PERMISSIONS_PATH` | training | Igual que en los BFF: `iss`/`aud` esperados y ruta del contrato de permisos. |
| `PUBSUB_PROJECT_ID`, `PUBSUB_TOPIC`, `OUTBOX_BATCH_SIZE`, `OUTBOX_LEASE_SECONDS`, `WORKER_ID` | publicador de entrenamientos | Proyecto, tópico (por defecto `sportapp.entrenamientos.v1`), lote (100), arrendamiento (30 s) e identidad de la réplica; usa `DATABASE_WRITE_URL`. |
| `PUBSUB_PROJECT_ID`, `PROJECTOR_SUBSCRIPTION`, `CONSUMER_MAX_MESSAGES` | proyector de entrenamientos | Proyecto, suscripción (por defecto `proyector-entrenamientos-v1`) y mensajes simultáneos por réplica (10); usa `DATABASE_READ_URL`. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_CONNECT_TIMEOUT_SECONDS` | servicios con base | Límites del pool (5, 5 y 5 por defecto). |
| `APP_FACTORY` | imagen | Fábrica `modulo:funcion`; la fija el Dockerfile. |

Para ejecutar `users-management` en el host (`task.py run`), genere una clave local en una sola línea y péguela como valor de `JWT_PRIVATE_KEY` en `.env` (no la comparta ni la reutilice en otro ambiente):

```powershell
uv run python -c "import sys; sys.path.insert(0, 'scripts'); from components import new_signing_key_pem; print(new_signing_key_pem().strip().replace(chr(10), chr(92) + 'n'))"
```

CORS en `bff-web` (la web corre en otro origen, en local y en Cloud Run): permite `GET`, `POST` y `DELETE` (este último para revocar autorizaciones de HU010), acepta las cabeceras `Authorization`, `Content-Type`, `Idempotency-Key` y `X-Correlation-ID`, expone al navegador `Location`, `Idempotent-Replayed`, `Retry-After` y `X-Correlation-ID`, y admite credenciales (`Access-Control-Allow-Credentials: true`) para que el navegador envíe la cookie del refresh token; por eso solo acepta orígenes explícitos. En Kubernetes las variables se leen de las claves opcionales `CORS_ALLOWED_ORIGINS` y `REFRESH_COOKIE_SAME_SITE` del ConfigMap `sportapp-runtime`.

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
 │  publicadores outbox, consumidor de afiliación y proyectores de entrenamientos y eventos ──► pubsub   │
 │                                                                                                        │
 │  Jobs que se ejecutan una vez: 8 migraciones de base de datos + 4 preparaciones de Pub/Sub            │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Inventario exacto de lo que se crea (47 recursos):

| Tipo | Cantidad | Nombres |
| --- | ---: | --- |
| Namespace | 1 | `sportapp-local` |
| StatefulSet | 1 | `postgres` |
| Deployment | 18 | `bff-web`, `bff-mobile`, `users-management`, `affiliation-management`, `complementary-services`, `training-management`, `events-management`, `third-party-management`, `users-management-outbox-publisher`, `users-management-read-projector`, `affiliation-management-free-plan-consumer`, `affiliation-management-outbox-publisher`, `training-management-outbox-publisher`, `training-management-read-projector`, `events-management-outbox-publisher`, `events-management-read-projector`, `complementary-services-outbox-publisher`, `pubsub-emulator` |
| Job | 14 | `users-management-migrate-write`, `users-management-migrate-read`, `users-management-directory-backfill`, `affiliation-management-migrate-main`, `complementary-services-migrate-main`, `training-management-migrate-write`, `training-management-migrate-read`, `events-management-migrate-write`, `events-management-migrate-read`, `users-management-pubsub-setup`, `affiliation-management-pubsub-setup`, `training-management-pubsub-setup`, `events-management-pubsub-setup`, `complementary-services-pubsub-setup` |
| Service | 10 | uno por Deployment de aplicación, más `postgres` y `pubsub-emulator` |
| ConfigMap | 2 | `sportapp-runtime-<hash>` (configuración) y `postgres-init-<hash>` (script que crea las bases) |
| Secret | 2 | `sportapp-database-<hash>` (contraseñas del paso 9) y `sportapp-users-jwt-<hash>` (clave de firma de HU003) |

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

**Qué debe ver:** una línea `condition met` por cada Job (11) y por cada Deployment (14).

#### 11.5 Estado final esperado

```powershell
kubectl --context docker-desktop --namespace sportapp-local get pods
```

Debe ver:

- **18 pods `Running` con `READY 1/1`:** `postgres-0`, `pubsub-emulator-...`, `users-management-...`, `affiliation-management-...`, `complementary-services-...`, `training-management-...`, `events-management-...`, `third-party-management-...`, `bff-web-...`, `bff-mobile-...`, `users-management-outbox-publisher-...`, `affiliation-management-free-plan-consumer-...`, `affiliation-management-outbox-publisher-...`, `training-management-outbox-publisher-...`, `training-management-read-projector-...`, `events-management-outbox-publisher-...`, `events-management-read-projector-...` y `complementary-services-outbox-publisher-...`.
- **Pods `Completed`** de los 13 Jobs.
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
  "accepted_policies_version": "sportapp-c108-c108plus.v1"
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

> `accepted_policies_version` debe ser `sportapp-c108-c108plus.v1`, la versión compartida aprobada (y la configurada en el overlay local). La composición de la plataforma debe configurar ese mismo valor en `REGISTRATION_POLICIES_VERSION`.

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

## Integración continua, release y despliegue

Cuatro responsables, sin solaparse:

| Quién | Qué hace | Credenciales de GCP |
| --- | --- | --- |
| **GitHub Actions** (`ci.yml`, `release-validation.yml`) | Valida: formato, lint, tipos, secretos, seguridad, pruebas y construcción local de imágenes. Nunca publica ni despliega. | **Ninguna**: sin `google-github-actions/auth`, sin `id-token: write`, sin secretos ni variables de GCP. No ejecuta `gcloud`, `kubectl`, Terraform ni OpenTofu. |
| **Cloud Build** (`cloudbuild.release.yaml`) | Iniciado desde GCP por un trigger de tag SemVer: valida, prueba, construye una vez, publica en Artifact Registry y escribe `backend-release.json`. | Las de su propia cuenta de construcción, administrada en GCP. Este repositorio no aporta ninguna. |
| **`sport-app-platform` / Cloud Deploy** | Promueve los digests de `backend-release.json` a producción, con aprobación obligatoria. | Las suyas; fuera de este repositorio. |
| **Terraform** (en `sport-app-platform`) | Infraestructura, aplicada manualmente por el propietario. | Nunca se ejecuta desde este repositorio. |

### GitHub Actions (sin GCP)

`.github/workflows/ci.yml`:

| Evento | Checks |
| --- | --- |
| push a `feature/**`, `fix/**`, `chore/**`, `release/**`, `hotfix/**` | `quality` (incluye el render de `deploy/k8s` con kustomize fijado por digest, sin `kubectl`), `unit`, `secrets` |
| pull request a `develop` o `main` | los anteriores más `integration-coverage` (PostgreSQL real, emulador de Pub/Sub, suites de integración, contrato y componente, umbral 80 %), `security` e `image (<componente>)` (construye sin publicar) |

`.github/workflows/release-validation.yml` (`release: types: [published]`): comprueba que el tag sea `vX.Y.Z` o `vX.Y.Z-pre` y que su commit pertenezca a `main`, repite calidad, seguridad, secretos y las cuatro suites con el umbral de cobertura, construye y prueba las ocho imágenes en el runner **sin publicarlas** y deja el artefacto `backend-validation-<tag>` (`backend-validation.json`: versión, commit, contratos, componentes con su `imageId` local y resultado de pruebas).

Un push o un merge a `develop` o `main` no publica ni despliega. `deliver-development.yml` se retiró el 2026-10-18: GCP no es un ambiente automático de desarrollo.

Las acciones están fijadas a SHA completo, con permisos mínimos (`contents: read`). Ningún workflow fusiona ramas.

### Cloud Build: construir y publicar (`cloudbuild.release.yaml`)

Pasos, en orden; cualquier fallo detiene el build:

1. `verify`: sustituciones obligatorias presentes, tag SemVer, tag → commit y commit dentro de `main`, contra una copia temporal sin archivos del repositorio (`scripts/release.py verify`).
2. `secrets`: Gitleaks sobre el árbol fuente.
3. `test-services`: PostgreSQL y el emulador de Pub/Sub en la red `cloudbuild`, con imágenes fijadas por digest.
4. `install`: `uv` 0.12.23 instalado con `--require-hashes` (`deploy/cloudbuild/uv-requirements.txt`) y el entorno desde `uv.lock`.
5. `quality`: `lock-check`, `format-check`, `lint`, `typecheck` y `security`.
6. `tests`: suites `unit`, `integration`, `contract` y `component`, más `coverage` con el umbral de `pyproject.toml` (80 %).
7. `k8s-render` y 8. `k8s-check`: render de `deploy/k8s/base` y del overlay local con kustomize, sin `kubectl` ni clúster; cada componente aparece y ninguna imagen es mutable.
9. `images`: cada componente se construye **una sola vez**, se prueba (importa su aplicación dentro de la imagen, sin red) y recibe las etiquetas `<tag>` y `<commit>`.
10. `push`: publica exactamente esas imágenes. Si una etiqueta ya existe en el registro, falla en vez de sobrescribirla. Resuelve el digest `sha256` de cada una.
11. `manifest`: escribe `dist/backend-release.json`. Cloud Build lo sube a `gs://<_RELEASE_BUCKET>/sport-app-backend/<tag>/` solo si todos los pasos terminaron bien.

`cloudbuild.release.yaml` **no** ejecuta Terraform, `gcloud run deploy/update`, `kubectl` ni credenciales de GKE; no crea releases ni rollouts de Cloud Deploy y no cambia IAM, APIs ni infraestructura. Las imágenes de todos los pasos están fijadas por digest y no se usa `latest`.

Sustituciones:

| Sustitución | Obligatoria | Valor |
| --- | --- | --- |
| `_RELEASE_TAG` | sí (por defecto `${TAG_NAME}` del trigger) | `vX.Y.Z` o `vX.Y.Z-pre` |
| `_RELEASE_COMMIT` | sí (por defecto `${COMMIT_SHA}`) | SHA completo del commit etiquetado |
| `_AR_LOCATION` | sí | región del repositorio de Artifact Registry (p. ej., `us-central1`) |
| `_AR_PROJECT` | no (por defecto `${PROJECT_ID}`) | proyecto del repositorio de Artifact Registry |
| `_AR_REPOSITORY` | sí | repositorio Docker de Artifact Registry |
| `_RELEASE_BUCKET` | sí | bucket de salida del manifiesto, aprovisionado previamente |
| `_SOURCE_REPOSITORY_URL` | no (por defecto el repositorio público) | URL desde la que `verify` comprueba tag y `main` |

Las imágenes quedan como `<_AR_LOCATION>-docker.pkg.dev/<_AR_PROJECT>/<_AR_REPOSITORY>/<componente>`. El build usa `options.logging: CLOUD_LOGGING_ONLY` (necesario con una cuenta de servicio propia) y `substitutionOption: MUST_MATCH`.

### `backend-release.json`

Estructura (los valores entre `<>` son ilustrativos):

```text
{
  "schemaVersion": 1,
  "kind": "backend-release",
  "repository": "https://github.com/lfnavarroj1/sport-app-backend",
  "version": "<X.Y.Z>",
  "tag": "<vX.Y.Z>",
  "commit": "<sha de 40 caracteres>",
  "contractVersion": "<info.version de bff-web>",
  "contracts": {"bff-web": "<versión>", "bff-mobile": "<versión>", "...": "los ocho componentes"},
  "builtAt": "<AAAA-MM-DDTHH:MM:SSZ>",
  "build": {"system": "cloud-build", "id": "<BUILD_ID>"},
  "images": {
    "users-management": "us-central1-docker.pkg.dev/<proyecto>/<repositorio>/users-management@sha256:<64 hex>",
    "...": "una URI por digest para cada uno de los ocho componentes"
  },
  "testResults": {
    "status": "passed",
    "suites": {"unit": {"status": "passed", "tests": <n>, "skipped": <n>}, "...": "..."},
    "coverage": {"line": <porcentaje>, "branch": <porcentaje>, "threshold": 80.0}
  }
}
```

- `contractVersion` es la versión del OpenAPI de `bff-web`, la que el frontend compara con su `expectedBffContract` (decisión del responsable, 2026-10-18); `contracts` trae la de los ocho componentes.
- `images` solo admite URI por digest: el manifiesto se rechaza si falta un componente o si una URI lleva una etiqueta. Las claves y el formato coinciden con `backendArtifacts` de `schemas/release-manifest.schema.json` de la plataforma.
- No contiene secretos ni etiquetas mutables.

### Lo que debe crear `sport-app-platform` (manual, fuera de este repositorio)

1. **Trigger de Cloud Build** sobre este repositorio, por tag con la expresión `^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$`, archivo `cloudbuild.release.yaml` y las sustituciones de la tabla.
2. **Cuenta de construcción exclusiva del backend**, asignada al trigger, con solo:
   - `roles/artifactregistry.writer` sobre **el** repositorio de Artifact Registry del backend (a nivel de repositorio, no de proyecto);
   - `roles/storage.objectCreator` sobre el bucket de salida, condicionado al prefijo `sport-app-backend/` (o un bucket dedicado);
   - `roles/logging.logWriter` para los registros del build.
3. **Bucket de salida** del manifiesto, con versionado o retención para que un `backend-release.json` publicado no se reemplace.
4. **Proceso de promoción** (Cloud Deploy) que lea `backend-release.json`, valide el esquema y los digests y despliegue esos mismos digests: Cloud Run para `bff-web` y `bff-mobile`, GKE para el resto. Sin recompilar.
5. **Aprobación obligatoria** antes de producción (`requireApproval` en el destino de Cloud Deploy).
6. Recomendado: etiquetas inmutables en el repositorio de Artifact Registry, para que ni un reintento manual pueda mover `<tag>` o `<commit>`.

La cuenta de construcción **no** debe tener Owner ni Editor, permisos de IAM, de red, de Cloud SQL ni para crear infraestructura. Tampoco debe poder impersonar `tf-apply-production` ni desplegar directamente en GKE o Cloud Run.

### Publicar una release

1. Integrar en `main` (PR de `release/**` o `hotfix/**`).
2. Crear la release en GitHub con un tag SemVer sobre ese commit, por ejemplo `gh release create v1.2.0 --target main --title v1.2.0`.
3. GitHub ejecuta `release-validation.yml`; su artefacto `backend-validation-v1.2.0` es la evidencia de GitHub.
4. El trigger de Cloud Build (creado por la plataforma) se activa con el mismo tag, publica las imágenes y escribe `backend-release.json`.
5. `sport-app-platform` promueve con aprobación. Este repositorio no despliega.

Validación local de los mismos pasos, sin GCP:

```powershell
py -3.13 -m uv run python scripts/task.py k8s-render --use-example --engine docker
py -3.13 scripts/release.py k8s-check reports/k8s/base.yaml
py -3.13 scripts/release.py images --tag v0.0.0-local --commit (git rev-parse HEAD) --plan dist/images-plan.json
```

`release.py images` sin `--registry` solo construye y prueba en local, y `release.py push` rechaza un plan sin registro.

## Documentación relacionada

- La arquitectura, las decisiones y los cursos de formación (uv, Terraform, GitHub Actions) están en la documentación central del proyecto (`documentacion/`), fuera de este repositorio.
- `scripts/task.py` también está documentado en la documentación central, en `integracion-continua/02-matriz-pipelines-por-repositorio.md`, subsección «Comandos del repositorio backend».

## Pendiente de decisión (no implementado)

- Ratificar ADR-019. Configurar los rulesets de `develop`/`main`, `CODEOWNERS` y aprobadores. Hoy solo existe la rama `master`.
- GCP (en `sport-app-platform`, aplicado manualmente): trigger y cuenta de Cloud Build del backend, repositorio de Artifact Registry, bucket del manifiesto y promoción con Cloud Deploy. GitHub no recibe credenciales de GCP (decisión del 2026-10-18).
- Promoción con Cloud Deploy, trigger y cuenta de Cloud Build, bucket de salida (todo en `sport-app-platform`); SBOM, procedencia y escaneo de imágenes antes de publicar.
- Contrato compartido de errores, sobre de eventos y primeros esquemas. Tópicos, suscripciones y DLQ.
- Proyector de lectura de usuarios (`usuarios-read-projector`): integrado en `develop` con HU005 (PR #21, `VERIFICADO_EN_CI`); falta desplegarlo en la composición de la plataforma y en GCP (suscripción `proyector-usuarios-v1`, DLQ, IAM) y ratificar su SLA de frescura. Los workers CQRS de eventos existen desde HU021 (CMP-004).
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
  - revocación por incidente, MFA y recuperación de cuenta (el cierre de sesión ya existe: HU004);
  - clientes web y móvil (almacenamiento seguro y E2E).
- HU021:
  - `T-HU021-WEB`: formulario `/organizador/eventos/nuevo` contra `bff-web` 0.10.0 (errores por campo, misma clave en los reintentos de red, `502`, `503` y `504`) y E2E vertical;
  - `T-HU021-PLAT`: tópico `sportapp.eventos.v1`, suscripción `proyector-eventos-v1` con orden, DLQ, reintentos, IAM, métricas y alerta en la composición local de la plataforma y en GCP (aquí solo existe la creación en el emulador);
  - catálogo público (HU022): debe reutilizar la regla de visibilidad de `event_read` (un `DRAFT` nunca es público); el calendario del organizador ya existe (HU023) y las consultas de sus eventos también (HU024);
  - tamaño de lote, arrendamiento, pool y HPA de los workers de eventos, y SLA de frescura de la proyección (CMP-004, no bloqueantes);
  - purga de claves de idempotencia vencidas, como en HU001, HU010 y HU015.
- HU015:
  - `T-HU015-WEB` (formulario y borrador) y su E2E;
  - purga de claves de idempotencia vencidas, como en HU001 y HU010.
- HU019:
  - `T-HU019-WEB`: publicar desde «mis servicios» contra `bff-web` 0.11.0 (misma clave en los reintentos de red, `502`, `503` y `504`; errores por campo) y su E2E vertical;
  - `T-HU019-PLAT`: tópico `sportapp.servicios.v1`, IAM del publicador, métricas de atraso de la outbox y la alerta sobre `outbox.publish_alert` en la composición local de la plataforma y en GCP (aquí solo existe la creación en el emulador);
  - alinear en la documentación los códigos `unauthenticated`/`forbidden`/`service_unavailable` de ADR-027 con los del repositorio, como en HU015 y HU021;
  - purga de claves de idempotencia vencidas, como en HU015.
- HU020:
  - `T-HU020-WEB`: opción «Servicios» del deportista en `/deportista/servicios` (filtros, intervalos en la zona del navegador, paginación, estado vacío y explicación de `restricted_categories`) contra `bff-web` 0.12.0, y su E2E;
  - la categoría médica con un deportista `PREMIUM` real ya se demuestra desde HU016 (prueba de componente `test_athlete_who_switches_to_premium_gains_sports_medicine`);
  - consumidor `FREE` operativo en la composición local para la capacidad.
- HU028:
  - `T-HU028-WEB`: `/profesional/entrenamientos/plantillas` (opción 125, `training.plan.author`) contra `bff-web` 0.24.0 (editor de sesiones semanales, `422` por campo e índice, lista propia, retiro con confirmación y `409` con recarga, la misma clave en los reintentos) y su E2E;
  - `T-HU028-MOB`: «Entrenamientos sugeridos» (opción `FREE` 130) contra `bff-mobile` 0.11.0 (lista y detalle con autor, nivel, semanas, sesiones y motivo; enlace informativo a la web; caché cifrada `STALE` 24 h) y su E2E;
  - plataforma: desplegar `training-management` 0.12.0 con las migraciones `0008_training_plan_templates` y de lectura `0010_training_plan_template_view` y el proyector compatible antes o junto con el productor, con `bff-web` 0.24.0 y `bff-mobile` 0.11.0;
  - ratificar los puntos «por ratificar» de los detalles de HU028;
  - purga de `command_idempotency` vencida (24 h), como las demás claves.
- HU026:
  - `T-HU026-WEB`: `/organizador/rutas` (opción 150, `training.routes.author`) contra `bff-web` 0.23.0 (formulario con coordenadas validadas, lista propia, retiro con confirmación y `409` con recarga, la misma clave en los reintentos) y su E2E;
  - `T-HU026-MOB`: «Rutas sugeridas» (opción `FREE` 110) contra `bff-mobile` 0.10.0 (motivos, no personalizada, intent `geo:`, aviso fijo de seguridad, caché Room cifrada `STALE` a las 24 h) y su E2E con una ruta publicada real;
  - plataforma: desplegar `training-management` 0.11.0 con las migraciones `0007_routes` y de lectura `0009_route_view` y el proyector compatible antes o junto con el productor de `ruta.*.v1`; `users-management` 0.12.0, `bff-web` 0.23.0 y `bff-mobile` 0.10.0. La imagen fija `ACTOR_PERMISSIONS_PATH` en la v2: si un ambiente lo sobrescribe con la v1, el organizador no podrá publicar rutas;
  - ratificar los puntos «por ratificar» de los detalles de HU026;
  - purga de `command_idempotency` vencida (24 h), como las demás claves.
- HU033:
  - `T-HU033-MOB`: feature `training-dashboard` (opción `FREE` 180) contra `bff-mobile` 0.9.0, con periodos 7/30/90/personalizado enviando el desfase del dispositivo (con `+` codificado), barras con alternativa textual, distancia `null` declarada como no disponible, caché Room cifrada por periodo (`STALE` 24 h) y su E2E;
  - HU031 (Strava): agregar `source` y la distancia a `training_session_history` para poblar `by_source.STRAVA` y `distance_meters`;
  - medición de rendimiento en un ambiente representativo (la local da p95 de 19–27 ms con 2.000 sesiones).
- HU034:
  - `T-HU034-MOB`: catálogo, formulario por metadatos, resultado con aviso localizado e historial con caché Room cifrada (`STALE` a las 24 h) y cola `PENDING_SYNC` contra `bff-mobile` 0.8.0 (CA6, CA8 y CA9), y su E2E;
  - HA014: corrida completa (10 min, 30 rps, p95 < 3 s y error < 1 %) **BLOQUEADA** hasta un ambiente representativo; la evidencia debe registrar revisión, dataset, recursos, p50/p95/p99 y error (`load/locust/training_metrics.py`);
  - plataforma: tópico `sportapp.entrenamientos.v1`, `proyector-entrenamientos-v1`, DLQ, IAM, métricas y alertas fuera del emulador, como en HU032; desplegar el proyector compatible antes que el productor de `indicador.calculado.v1` (ADR-030);
  - ratificar en ADR-030 las causas `invalid_uuid` e `invalid_cursor`, que no están en su catálogo;
  - purga de `command_idempotency` vencida (30 días), como las demás claves.
  - Verificado el 2026-10-18 en `sportapp-local`: el consumidor `FREE` activa la capacidad y el recorrido por `bff-mobile` 0.8.0 pasa de punta a punta (CA1–CA5) con el publicador y el proyector de Entrenamientos.
- HU024:
  - `T-HU024-WEB`: `/organizador/eventos/publicacion` contra `bff-web` 0.16.0 (lista con `GET /v1/me/events`, detalle, `422` por campo, confirmación y la misma clave en los reintentos) y E2E vertical;
  - plataforma: desplegar el proyector compatible antes que el productor de `evento.publicado.v1` (ADR-033) y `proyector-eventos-v1` con su DLQ fuera del emulador;
  - SLA de frescura de la proyección (CMP-004) sin ratificar: entre la publicación y la proyección, la lista y el calendario muestran el estado anterior.
- HU006:
  - `T-HU006-WEB`: sección «Hábitos deportivos» de `/deportista/perfil` contra `bff-web` 0.19.0 (editor con catálogos cerrados, `409` con recarga, `422` por campo e índice, la misma clave en los reintentos) y su E2E;
  - plataforma: desplegar `users-management` 0.9.0 (migración `0005_sport_habits`) y el proyector compatible antes o junto con el productor;
  - HU008 (actualización parcial) reutilizará el agregado y el evento;
  - purga de claves de idempotencia vencidas, como en las demás historias.
- HU009:
  - `T-HU009-WEB`: sección «Hábitos alimenticios» de `/deportista/perfil` contra `bff-web` 0.21.0 (consentimiento con la versión configurada, catálogos cerrados sin repetidos, hidratación como texto decimal, `409` con recarga, `422` por campo e índice, la misma clave en los reintentos) y su E2E;
  - plataforma: desplegar `users-management` 0.10.0 (migraciones `0006_nutrition_habits` y de lectura `0005_nutrition_habits_view`) con el proyector compatible antes o junto con el productor; `HEALTH_DATA_CONSENT_VERSION` ya es obligatoria desde HU005;
  - ratificar el «sin hábitos» `200` frente al `404 nutrition_habits_not_found` de HU009 CA5;
  - purga de claves de idempotencia vencidas, como en las demás historias.
- HU016:
  - `T-HU016-WEB`: «Mi plan» en `/deportista/plan` contra `bff-web` 0.20.0 (comparación por capacidades y ofertas, confirmación con el aviso de no cobro, `409` recuperables y la misma clave en los reintentos, menú reconstruido con `GET /v1/me/capabilities`) y su E2E;
  - plataforma: `USERS_MANAGEMENT_BASE_URL` para `affiliation-management` en `cloud_run_env` (en Kubernetes ya sale de `sportapp-runtime`) y desplegar la migración `0004_subscription_changes` antes de las réplicas;
  - purga de `command_idempotency` vencida (24 h), como las demás claves.
- HU007:
  - `T-HU007-WEB`: sección «Estado de cuenta» del perfil de cada actor (y enlace desde «Mi plan») contra `bff-web` 0.25.0 (importes COP en `es-CO`, fechas en la zona del navegador, paginación por cursor, `PROVISIONING`/`NOT_APPLICABLE` como estados y no como error, aviso «SportApp no registra pagos») y su E2E;
  - plataforma: desplegar `affiliation-management` 0.4.0 con `bff-web` 0.25.0 (sin migraciones ni variables nuevas);
  - ratificar los puntos «por ratificar» de los detalles de HU007 (`NONE` y compromisos «no cerrados»).
- HU014:
  - `T-HU014-WEB`: `/profesional/alimentacion/planes` (editor con el catálogo, errores por ítem, aviso `nutrition_habits_missing`) y la sección «Planes de alimentación» de `/deportista/alimentacion` contra `bff-web` 0.18.0, con su E2E (plan bloqueado y luego válido);
  - el bloqueo con hábitos reales ya es posible desde HU009 (backend); falta que web registre los hábitos (`T-HU009-WEB`);
  - ratificar el catálogo de 41 alimentos, sus unidades y `incompatible_with` (cambiarlo es otra migración);
  - purga de claves de idempotencia vencidas, como en las demás historias.
- HU022:
  - `T-HU022-WEB`: `/organizador/eventos/inscritos` contra `bff-web` 0.22.0 (selector de evento propio con `GET /v1/me/events`, conteos, filtro, paginación y «nombre no disponible» cuando `display_name` es `null`) y su E2E con una inscripción real de HU041;
  - plataforma: desplegar `events-management` 0.7.0 con la migración de lectura `0006_registration_event_order`, `users-management` 0.11.0 y `bff-web` 0.22.0 juntos (el BFF usa el contrato interno nuevo);
  - ratificar los puntos «por ratificar» de los detalles de HU022.
- HU023:
  - `T-HU023-WEB`: `/organizador/eventos/calendario` contra `bff-web` 0.17.0 (mes y lista, filtros, zona del navegador con la zona del evento visible, selector de evento propio que resalta las coincidencias) y E2E con un borrador contra dos publicados (uno coincide por ciudad, otro no);
  - plataforma: desplegar `events-management` 0.6.0 y `bff-web` 0.17.0 juntos (el BFF usa los filtros nuevos del servicio);
  - sin SLA de frescura (CMP-004): un evento recién publicado aparece en el calendario cuando se proyecta.
- HU041:
  - `T-HU041-MOB`: detalle, botón de inscripción con clave conservada en reintentos y «mis inscripciones» con caché Room cifrada (`STALE` 24 h) contra `bff-mobile` 0.7.0, y su E2E;
  - el E2E vertical depende de integrar el backend de HU024 (`VERIFICADO_LOCALMENTE` el 2026-10-18) y de publicar el evento por API o con `T-HU024-WEB`;
  - plataforma: `proyector-eventos-v1` y su DLQ fuera del emulador, como en HU021;
  - riesgo de clave caliente en eventos muy demandados (bloqueo de la fila del evento) para HA017;
  - purga de claves de idempotencia vencidas, como en las demás historias.
- HU035:
  - `T-HU035-MOB`: pantalla `/calendario` con caché Room cifrada, `STALE` a las 24 h y estados de fuente incompleta contra `bff-mobile` 0.6.0, y su E2E;
  - dependencias para la aceptación (ADR-031): consumidor `FREE` operativo, `usuarios-read-projector`, HU024 (publicación de eventos) y `T-HU010-WEB`;
  - ADR-031 no fija qué responder si llega solo uno de `from`/`to`: se respondió `422 required`;
  - inscripción del deportista a eventos: cubierta por HU041 (ADR-032).
- HU032:
  - `T-HU032-MOB`: pantalla `/entrenamientos/sesion` offline first (Room, WorkManager, cifrado con Keystore, orden inicio→fin, expiración de 30 días) contra `bff-mobile` 0.5.0, y su E2E sin red;
  - plataforma: confirmar que tópico `sportapp.entrenamientos.v1`, `proyector-entrenamientos-v1`, DLQ, IAM, métricas y alertas existan fuera del emulador antes del E2E desplegado;
  - desplegar el proyector compatible antes que el productor del evento de inicio (ADR-029);
  - purga de `command_idempotency` vencida (30 días), como las demás claves.
- HU011:
  - `T-HU011-WEB` (línea de tiempo con filtros y paginación) y la opción del historial en el menú del deportista, que la navegación vigente solo asigna al profesional;
  - ratificar el SLA de frescura de la proyección (una revocación se aplica con el retraso del proyector);
  - CA5: `T-HU011-WEB` debe consumir `GET /v1/me/training-athletes` (selector del deportista); la plataforma debe ejecutar el Job `users-management-directory-backfill` después de la migración `0004` en cada ambiente con datos previos.
- HU010:
  - desplegar `usuarios-read-projector` (implementado en HU005): sin él en la composición, `POST /v1/users/lookup` no resuelve a nadie y el flujo web no puede autorizar ni asignar. El recorrido completo por Pub/Sub ya está verificado en `bff/web/tests/component/test_training_plan_vertical_flow.py` (2026-10-17). En la composición local siguen faltando, a cargo de la plataforma: recrear los recursos del emulador después de cada reinicio (guarda todo en memoria), alinear sus nombres con `contracts/events/README.md` (`proyector-usuarios-v1`, `proyector-entrenamientos-v1`, `sportapp.<dominio>.proyeccion.dlq`, `sportapp.afiliacion.alta-gratuita.v1` con filtro) y `HEALTH_DATA_CONSENT_VERSION` en el ConfigMap. Si falta su suscripción, los proyectores de Usuarios y Entrenamientos y el consumidor `FREE` registran `*.subscription_failed` (`error_type`, p. ej. `NotFound`) y salen con código 1;
  - `T-HU010-WEB`: pantallas de autorización (deportista) y de creación/asignación (profesional) contra `bff-web` 0.6.0, y E2E vertical;
  - `T-HU010-PLAT`: tópico `sportapp.entrenamientos.v1`, suscripción `proyector-entrenamientos-v1` con orden, DLQ, reintentos, IAM, métricas y alerta en la composición local de la plataforma y en GCP (aquí solo existe la creación en el emulador);
  - ratificar los reintentos del proyector (10 intentos, backoff 10–600 s), el SLA de frescura de la vista y el tamaño de lote/pool del perfil de entrenamientos (CMP-003);
  - `POST /v1/me/training-authorizations` responde `404 professional_not_found`, lo que permite a un deportista saber si un correo es de un profesional (riesgo aceptado o mitigación pendiente);
  - listados (planes de un deportista, autorizaciones vigentes) y edición/versionado de planes quedan fuera de HU010;
  - purga de claves de idempotencia vencidas, igual que en HU001.
- HU004:
  - `T-HU004-WEB` y `T-HU004-MOB` (contratos `bff-web` 0.7.0 y `bff-mobile` 0.4.0 disponibles) y los E2E de ambos canales;
  - revocación inmediata de access tokens: descartada por ahora (vigencia corta de 15 min); revisar si cambia la política de seguridad;
  - purga de refresh tokens revocados o vencidos (pendiente compartido con HU003).
- Ubicación ratificada de Playwright para API/E2E. Las pruebas de componente BFF → servicio ya existen para HU001 (`bff/web/tests/component`).
