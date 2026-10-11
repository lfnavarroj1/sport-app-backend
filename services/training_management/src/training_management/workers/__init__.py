"""Cargas asíncronas de `training_management`: publicador de outbox y proyector de lectura.

Se implementan cuando existan tópico, payload, ordering key, reintentos y DLQ
versionados (CMP-002); hasta entonces no existe ningún worker desplegable.
"""
