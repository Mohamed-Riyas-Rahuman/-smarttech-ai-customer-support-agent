"""Allow tickets from senders without a verified customer record."""
from alembic import op
import sqlalchemy as sa


revision = "b73d9c2e4a10"
down_revision = "a92b7641c3ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "support_tickets",
        "customer_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # PostgreSQL rejects this if unverified tickets exist, which is safer than
    # silently assigning them to an unrelated customer.
    op.alter_column(
        "support_tickets",
        "customer_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
