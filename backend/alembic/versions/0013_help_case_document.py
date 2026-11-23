"""Link a resolved help request to the case document drafted from it."""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_help_request", sa.Column("case_document_id", sa.String(32),
                                                sa.ForeignKey("knv_document.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("knv_help_request", "case_document_id")
