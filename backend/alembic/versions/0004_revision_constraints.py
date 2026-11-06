"""Enforce document identity and a single published revision per document.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Let constraint creation fail transactionally on inconsistent legacy data;
    # never select a winner, retire revisions or merge documents automatically.
    op.create_unique_constraint("uq_knv_document_kb_title", "knv_document", ["kb_id", "title"])
    op.create_index("uq_knv_revision_published", "knv_revision", ["document_id"],
                    unique=True, postgresql_where=sa.text("status = 'PUBLISHED'"))


def downgrade() -> None:
    op.drop_index("uq_knv_revision_published", table_name="knv_revision")
    op.drop_constraint("uq_knv_document_kb_title", "knv_document", type_="unique")
