"""Long-term episodic memory and user profiles (v4 D12)."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knv_episode",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("knv_user.id"), nullable=False, index=True),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64)),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("refs", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("dense", Vector(1024), nullable=False),  # BGE-M3 size, as knv_chunk.dense (0001)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp()),
        sa.UniqueConstraint("user_id", "source_kind", "source_id", name="uq_knv_episode_source"),
        sa.CheckConstraint("source_kind IN ('answer', 'help_request')", name="ck_knv_episode_kind"),
    )
    op.create_table(
        "knv_profile",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("knv_user.id"), primary_key=True),
        sa.Column("lang_counts", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("model_counts", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("machine_counts", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp()),
    )


def downgrade() -> None:
    op.drop_table("knv_profile")
    op.drop_table("knv_episode")
