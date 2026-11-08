"""Timestamp new escalations; preserve unknown dates on historical records."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_escalation", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("knv_escalation", "created_at", server_default=sa.text("clock_timestamp()"))
    op.create_index("ix_knv_escalation_user_created", "knv_escalation",
                    ["user_id", sa.text("created_at DESC NULLS LAST"), sa.text("id DESC")])


def downgrade() -> None:
    op.drop_index("ix_knv_escalation_user_created", table_name="knv_escalation")
    op.drop_column("knv_escalation", "created_at")
