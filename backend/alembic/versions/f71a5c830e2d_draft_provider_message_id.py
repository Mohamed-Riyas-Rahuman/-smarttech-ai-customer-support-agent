"""Record the confirmed Gmail message ID on sent drafts."""
from alembic import op
import sqlalchemy as sa

revision = "f71a5c830e2d"
down_revision = "e48c12a9f731"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("drafts", sa.Column("provider_message_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("drafts", "provider_message_id")
