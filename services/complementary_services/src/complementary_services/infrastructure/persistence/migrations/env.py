from alembic import context

from complementary_services.infrastructure.persistence.models import Base
from sportapp_platform.migrations import run_migrations

run_migrations(context, Base.metadata)
