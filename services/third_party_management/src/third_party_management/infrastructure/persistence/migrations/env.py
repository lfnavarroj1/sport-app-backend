from alembic import context

from sportapp_platform.migrations import run_migrations
from third_party_management.infrastructure.persistence.models import Base

run_migrations(context, Base.metadata)
