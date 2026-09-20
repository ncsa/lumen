"""Retain inactive endpoints for historical request attribution.

Revision ID: d0e1f2a3b4c5
Revises: c9d8e7f6a5b4
"""

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_endpoints", sa.Column(
        "active", sa.Boolean(), nullable=False, server_default=sa.true(),
        comment="Whether the endpoint is configured for live use; inactive rows preserve request history",
    ))


def downgrade():
    op.drop_column("model_endpoints", "active")
