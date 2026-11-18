"""Preserve RFC message threading headers from inbound email."""
from alembic import op
import sqlalchemy as sa

revision = "e48c12a9f731"
down_revision = "d2a94f17c610"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("rfc_message_id", sa.String(length=998), nullable=True))
    op.add_column("email_messages", sa.Column("references_header", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "references_header")
    op.drop_column("email_messages", "rfc_message_id")
