"""Cargas asíncronas de `users_management` (CMP-001).

- `outbox_publisher`: `usuarios-outbox-publisher`, publica la outbox en `sportapp.usuarios.v1`.
- `pubsub_setup`: crea el tópico solo en el emulador local/CI.

El proyector de lectura (`usuarios-read-projector`) todavía no existe: requiere
suscripción, DLQ y vistas de lectura aprobadas.
"""
