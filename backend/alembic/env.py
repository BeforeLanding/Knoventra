from alembic import context
from sqlalchemy import create_engine

from knoventra.config import settings
from knoventra.db.models import Base

target_metadata = Base.metadata


def run() -> None:
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run()
