"""Cargas asíncronas de `users_management` (CMP-001).

- `outbox_publisher`: `usuarios-outbox-publisher`, publica la outbox en `sportapp.usuarios.v1`.
- `read_projector`: `usuarios-read-projector`, consume `proyector-usuarios-v1` y mantiene
  `user_directory` y `athlete_profile_view` en la base de lectura.
- `pubsub_setup`: crea el tópico, la suscripción del proyector y su DLQ solo en el
  emulador local/CI.
"""
