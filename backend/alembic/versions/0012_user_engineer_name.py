"""Link user accounts to skill-matrix engineers for expert recommendation."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_user", sa.Column("engineer_name", sa.String(128), nullable=True))
    op.create_unique_constraint("knv_user_engineer_name_key", "knv_user", ["engineer_name"])


def downgrade() -> None:
    op.drop_constraint("knv_user_engineer_name_key", "knv_user")
    op.drop_column("knv_user", "engineer_name")
