"""Skill matrix imported from technical-journal workbooks."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knv_skill",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("machine_id", sa.String(64), nullable=False),
        sa.Column("engineer_name", sa.String(128), nullable=False),
        sa.Column("skill_key", sa.String(255), nullable=False),
        sa.Column("skill_item", sa.String(255), nullable=False),
        sa.Column("op_count", sa.Integer, nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("company", sa.String(128), nullable=False, server_default=""),
        sa.Column("source_file", sa.String(255), nullable=False),
        sa.Column("source_row", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("machine_id", "engineer_name", "skill_key", name="uq_knv_skill_machine_engineer_key"),
        sa.CheckConstraint("level IN ('none', 'familiar', 'proficient')", name="ck_knv_skill_level"),
        sa.CheckConstraint("op_count >= 0", name="ck_knv_skill_op_count"),
    )
    op.create_index("ix_knv_skill_key_level", "knv_skill", ["skill_key", "level"])


def downgrade() -> None:
    op.drop_index("ix_knv_skill_key_level", table_name="knv_skill")
    op.drop_table("knv_skill")
