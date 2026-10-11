from alembic import context

from sportapp_platform.migrations import run_migrations
from users_management.infrastructure.persistence_read.models import ReadBase

run_migrations(context, ReadBase.metadata)
