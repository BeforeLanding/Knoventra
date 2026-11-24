"""Confirmations carry the exact payload to file (v4 D13 two-phase writes)."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_confirmation", sa.Column("payload", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("knv_confirmation", "payload")
