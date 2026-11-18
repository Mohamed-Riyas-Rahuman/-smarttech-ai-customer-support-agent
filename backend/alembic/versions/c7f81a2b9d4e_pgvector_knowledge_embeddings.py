"""Track embedding provenance and index PostgreSQL knowledge vectors.

Revision ID: c7f81a2b9d4e
Revises: b6a326231773
Create Date: 2026-11-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "c7f81a2b9d4e"
down_revision: Union[str, Sequence[str], None] = "b6a326231773"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add pgvector support without recreating tables or removing chunk data."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_chunks")}

    if "embedding" not in columns:
        op.add_column(
            "knowledge_chunks",
            sa.Column("embedding", Vector(), nullable=True),
        )
    if "embedding_model" not in columns:
        op.add_column(
            "knowledge_chunks",
            sa.Column("embedding_model", sa.String(length=255), nullable=True),
        )
    if "embedding_dimension" not in columns:
        op.add_column(
            "knowledge_chunks",
            sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        )

    # Existing vectors were produced with the configured 384-dimension
    # sentence-transformers/all-MiniLM-L6-v2 model. Preserve them and record
    # their provenance; unembedded chunks remain explicitly marked for reindex.
    op.execute(
        sa.text(
            """
            UPDATE knowledge_chunks
            SET embedding_model = CASE
                    WHEN embedding IS NULL THEN 'unembedded'
                    ELSE 'sentence-transformers/all-MiniLM-L6-v2'
                END,
                embedding_dimension = CASE
                    WHEN embedding IS NULL THEN 0
                    ELSE vector_dims(embedding)
                END
            WHERE embedding_model IS NULL OR embedding_dimension IS NULL
            """
        )
    )
    op.alter_column(
        "knowledge_chunks",
        "embedding_model",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "knowledge_chunks",
        "embedding_dimension",
        existing_type=sa.Integer(),
        nullable=False,
    )

    # HNSW supports up to 2,000 dimensions. The expression index keeps the
    # underlying vector column dimension-flexible; other configured dimensions
    # use exact cosine search until a matching expression index is added.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_hnsw_384
        ON knowledge_chunks USING hnsw
        ((embedding::vector(384)) vector_cosine_ops)
        WHERE embedding IS NOT NULL AND embedding_dimension = 384
        """
    )


def downgrade() -> None:
    """Remove the optional ANN index while preserving every stored embedding."""
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw_384")
    # The vector and provenance columns intentionally remain to avoid deleting
    # application data during a schema downgrade.
