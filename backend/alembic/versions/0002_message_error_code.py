"""Preserve refusal and failure state in conversation replay.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_message", sa.Column("error_code", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("knv_message", "error_code")
