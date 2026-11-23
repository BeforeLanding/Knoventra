"""Transactional outbox for Elasticsearch keyword-index sync."""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knv_es_outbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # No foreign key: a deleted document's row must survive so drain can purge its ES copy.
        sa.Column("document_id", sa.String(32), nullable=False, unique=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("knv_es_outbox")
