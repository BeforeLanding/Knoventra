"""Help requests: v3 escalations become kind=expert_escalation; append-only event log.

The table is renamed in place, so existing rows, ids and the (user_id,
operation_id) idempotency key carry over unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

STATUSES = "'OPEN', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'CLOSED', 'CANCELLED'"


def upgrade() -> None:
    op.rename_table("knv_escalation", "knv_help_request")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_escalation_pkey TO knv_help_request_pkey")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_escalation_user_id_operation_id_key "
               "TO knv_help_request_user_id_operation_id_key")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_escalation_user_id_fkey "
               "TO knv_help_request_user_id_fkey")
    op.execute("ALTER INDEX ix_knv_escalation_user_id RENAME TO ix_knv_help_request_user_id")
    op.execute("ALTER INDEX ix_knv_escalation_user_created RENAME TO ix_knv_help_request_user_created")

    add = op.add_column
    add("knv_help_request", sa.Column("kind", sa.String(24), nullable=False, server_default="expert_escalation"))
    add("knv_help_request", sa.Column("machine_id", sa.String(64), nullable=True))
    add("knv_help_request", sa.Column("machine_model", sa.String(16), nullable=True))
    add("knv_help_request", sa.Column("alarm_codes", sa.JSON, nullable=False, server_default="[]"))
    add("knv_help_request", sa.Column("symptom", sa.Text, nullable=False, server_default=""))
    add("knv_help_request", sa.Column("tried_steps", sa.Text, nullable=False, server_default=""))
    add("knv_help_request", sa.Column("assignee_id", sa.String(32), sa.ForeignKey("knv_user.id"), nullable=True))
    add("knv_help_request", sa.Column("resolution", sa.Text, nullable=False, server_default=""))
    add("knv_help_request", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                                      server_default=sa.text("clock_timestamp()")))
    add("knv_help_request", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    add("knv_help_request", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_knv_help_request_assignee_id", "knv_help_request", ["assignee_id"])
    op.create_check_constraint("ck_knv_help_request_status", "knv_help_request", f"status IN ({STATUSES})")
    op.create_check_constraint("ck_knv_help_request_kind", "knv_help_request",
                               "kind IN ('expert_escalation', 'field_help')")

    op.create_table(
        "knv_help_event",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(32), sa.ForeignKey("knv_help_request.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("actor_id", sa.String(32), sa.ForeignKey("knv_user.id"), nullable=True),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
    )
    op.create_index("ix_knv_help_event_request_id", "knv_help_event", ["request_id"])
    # Existing escalations get a creation event so every request has a complete trail.
    op.execute("INSERT INTO knv_help_event (request_id, actor_id, action, from_status, to_status, note, at) "
               "SELECT id, user_id, 'created', NULL, status, 'migrated from v3 escalation', "
               "COALESCE(created_at, clock_timestamp()) FROM knv_help_request")
    op.execute("""
        CREATE FUNCTION knv_help_event_append_only() RETURNS trigger AS $$
        BEGIN
            -- Events are immutable while their request exists. Purging a whole request
            -- (cascade) is the only way an event row can disappear.
            IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM knv_help_request WHERE id = OLD.request_id) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'knv_help_event is append-only';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER knv_help_event_no_change BEFORE UPDATE OR DELETE ON knv_help_event "
               "FOR EACH ROW EXECUTE FUNCTION knv_help_event_append_only()")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT count(*) FROM knv_help_request WHERE kind <> 'expert_escalation'")).scalar():
        raise RuntimeError("field help requests exist; refusing a downgrade that would lose them")
    op.execute("DROP TRIGGER knv_help_event_no_change ON knv_help_event")
    op.execute("DROP FUNCTION knv_help_event_append_only()")
    op.drop_index("ix_knv_help_event_request_id", table_name="knv_help_event")
    op.drop_table("knv_help_event")
    op.drop_constraint("ck_knv_help_request_kind", "knv_help_request")
    op.drop_constraint("ck_knv_help_request_status", "knv_help_request")
    op.drop_index("ix_knv_help_request_assignee_id", table_name="knv_help_request")
    for column in ("closed_at", "resolved_at", "updated_at", "resolution", "assignee_id", "tried_steps", "symptom",
                   "alarm_codes", "machine_model", "machine_id", "kind"):
        op.drop_column("knv_help_request", column)
    op.execute("ALTER INDEX ix_knv_help_request_user_created RENAME TO ix_knv_escalation_user_created")
    op.execute("ALTER INDEX ix_knv_help_request_user_id RENAME TO ix_knv_escalation_user_id")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_help_request_user_id_fkey "
               "TO knv_escalation_user_id_fkey")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_help_request_user_id_operation_id_key "
               "TO knv_escalation_user_id_operation_id_key")
    op.execute("ALTER TABLE knv_help_request RENAME CONSTRAINT knv_help_request_pkey TO knv_escalation_pkey")
    op.rename_table("knv_help_request", "knv_escalation")
