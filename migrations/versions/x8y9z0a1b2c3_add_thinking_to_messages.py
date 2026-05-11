"""Add thinking content and thinking_tokens columns to messages

Revision ID: x8y9z0a1b2c3
Revises: w3x4y5z6a7b8
Create Date: 2026-05-09 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "x8y9z0a1b2c3"
down_revision = "w3x4y5z6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("thinking", sa.Text(), nullable=True,
                  comment="Reasoning/thinking content from the model; assistant messages only"))
    op.add_column("messages", sa.Column("thinking_tokens", sa.Integer(), nullable=True,
                  comment="Thinking/reasoning token count reported by the model; assistant messages only"))


def downgrade():
    op.drop_column("messages", "thinking_tokens")
    op.drop_column("messages", "thinking")
