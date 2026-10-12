from alembic import context

from events_management.infrastructure.persistence_read.models import ReadBase
from sportapp_platform.migrations import run_migrations

run_migrations(context, ReadBase.metadata)
