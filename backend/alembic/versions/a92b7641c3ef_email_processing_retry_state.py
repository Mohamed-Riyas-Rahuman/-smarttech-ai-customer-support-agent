"""Persist generation failures and bound automatic inbox retries."""
from alembic import op
import sqlalchemy as sa

revision = "a92b7641c3ef"
down_revision = "f71a5c830e2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("processing_status", sa.String(length=30), nullable=False, server_default="received"))
    op.add_column("email_messages", sa.Column("processing_error", sa.Text(), nullable=True))
    op.add_column("email_messages", sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("email_messages", sa.Column("next_processing_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "next_processing_attempt_at")
    op.drop_column("email_messages", "processing_attempts")
    op.drop_column("email_messages", "processing_error")
    op.drop_column("email_messages", "processing_status")
