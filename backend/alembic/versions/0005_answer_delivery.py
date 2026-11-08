"""Track web turns and idempotent final answer delivery without backfilling history."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_conversation", sa.Column("active_turn_id", sa.String(32), nullable=True))
    op.add_column("knv_message", sa.Column("completion_turn_id", sa.String(32), nullable=True))
    op.create_unique_constraint("uq_knv_message_completion", "knv_message", ["conversation_id", "completion_turn_id"])


def downgrade() -> None:
    op.drop_constraint("uq_knv_message_completion", "knv_message", type_="unique")
    op.drop_column("knv_message", "completion_turn_id")
    op.drop_column("knv_conversation", "active_turn_id")
