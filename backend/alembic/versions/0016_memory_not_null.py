"""Align memory tables with the ORM: timestamps are NOT NULL (they always had server defaults)."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

COLUMNS = (("knv_episode", "created_at"), ("knv_episode", "updated_at"), ("knv_profile", "updated_at"))


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = clock_timestamp() WHERE {column} IS NULL")
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=True)
