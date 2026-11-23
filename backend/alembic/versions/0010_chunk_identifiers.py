"""Exact-code recall: canonical identifiers per chunk with a GIN index.

Backfill uses the application's identifier grammar so stored values match what
new ingestion writes. If that grammar changes, re-run the backfill.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knv_chunk", sa.Column("identifiers", postgresql.ARRAY(sa.String(64)), nullable=False,
                                         server_default="{}"))
    from knoventra.retrieval.identifiers import evidence_identifiers

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, section_path, text FROM knv_chunk")).all()
    for row in rows:
        codes = sorted(evidence_identifiers(row.section_path, row.text))
        if codes:
            conn.execute(sa.text("UPDATE knv_chunk SET identifiers = :codes WHERE id = :id"),
                         {"codes": codes, "id": row.id})
    op.create_index("ix_knv_chunk_identifiers", "knv_chunk", ["identifiers"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_knv_chunk_identifiers", table_name="knv_chunk")
    op.drop_column("knv_chunk", "identifiers")
