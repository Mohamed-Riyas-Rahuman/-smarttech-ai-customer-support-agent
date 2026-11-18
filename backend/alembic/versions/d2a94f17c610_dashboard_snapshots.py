"""Add aggregate dashboard snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "d2a94f17c610"
down_revision = "c7f81a2b9d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_type", sa.String(length=20), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in (
            "incoming_emails", "processed_emails", "ai_successes", "ai_failures",
            "pending_responses", "tickets_awaiting_approval", "sent_responses",
            "failed_sends", "attention_needed", "high_priority", "escalated",
        )],
    )
    op.create_index("ix_dashboard_snapshots_created_at", "dashboard_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_dashboard_snapshots_created_at", table_name="dashboard_snapshots")
    op.drop_table("dashboard_snapshots")
