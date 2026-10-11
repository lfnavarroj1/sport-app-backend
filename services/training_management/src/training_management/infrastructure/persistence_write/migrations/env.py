from alembic import context

from sportapp_platform.migrations import run_migrations
from training_management.infrastructure.persistence_write.models import WriteBase

run_migrations(context, WriteBase.metadata)
