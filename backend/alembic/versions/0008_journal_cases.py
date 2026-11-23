"""Deduplicated field cases from technical journals, plus an ingest audit log."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knv_journal_case",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("text_key", sa.String(64), nullable=False, unique=True),
        sa.Column("simhash", sa.BigInteger, nullable=False),
        sa.Column("models", sa.JSON, nullable=False),
        sa.Column("models_key", sa.String(128), nullable=False),
        sa.Column("model_source", sa.String(16), nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("symptom", sa.Text, nullable=False),
        sa.Column("alarm_codes", sa.JSON, nullable=False),
        sa.Column("cause", sa.Text, nullable=False),
        sa.Column("solution", sa.Text, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=True),
        sa.Column("machine_ids", sa.JSON, nullable=False),
        sa.Column("engineers", sa.JSON, nullable=False),
        sa.Column("companies", sa.JSON, nullable=False),
        sa.Column("regions", sa.JSON, nullable=False),
        sa.Column("dates", sa.JSON, nullable=False),
        sa.Column("sources", sa.JSON, nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False),
        sa.Column("first_date", sa.Date, nullable=False),
        sa.Column("last_date", sa.Date, nullable=False),
        sa.CheckConstraint("category IN ('troubleshooting', 'procedure')", name="ck_knv_journal_case_category"),
    )
    op.create_index("ix_knv_journal_case_models_key", "knv_journal_case", ["models_key"])
    op.create_table(
        "knv_ingest_audit",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("files", sa.JSON, nullable=False),
        sa.Column("counts", sa.JSON, nullable=False),
        sa.Column("redactions", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
    )


def downgrade() -> None:
    op.drop_table("knv_ingest_audit")
    op.drop_index("ix_knv_journal_case_models_key", table_name="knv_journal_case")
    op.drop_table("knv_journal_case")
