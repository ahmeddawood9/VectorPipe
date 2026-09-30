"""create documents table

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("raw_object_key", sa.String(length=512), nullable=False),
        sa.Column("processed_object_key", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "content_type", sa.String(length=255), server_default="application/octet-stream", nullable=False
        ),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')", name=op.f("ck_documents_status_valid")
        ),
    )
    op.create_index("ix_documents_status_created_at", "documents", ["status", "created_at"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_completed_at", "documents", ["completed_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_completed_at", table_name="documents")
    op.drop_index("ix_documents_created_at", table_name="documents")
    op.drop_index("ix_documents_status_created_at", table_name="documents")
    op.drop_table("documents")
