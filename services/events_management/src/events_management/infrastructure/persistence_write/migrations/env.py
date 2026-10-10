from alembic import context

from events_management.infrastructure.persistence_write.models import WriteBase
from sportapp_platform.migrations import run_migrations

run_migrations(context, WriteBase.metadata)
