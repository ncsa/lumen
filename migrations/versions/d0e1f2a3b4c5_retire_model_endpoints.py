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


def _is_postgresql():
    return op.get_bind().dialect.name == "postgresql"


def upgrade():
    op.add_column("model_endpoints", sa.Column(
        "active", sa.Boolean(), nullable=False, server_default=sa.true(),
        comment="Whether the endpoint is configured for live use; inactive rows preserve request history",
    ))
    # Keep the column comment in step with the ORM model: the reference is
    # stable because endpoints are retired, not deleted (see RequestLog).
    if _is_postgresql():
        op.execute(sa.text(
            "COMMENT ON COLUMN request_logs.model_endpoint_id IS "
            "'Backend endpoint that served the request; endpoints are retired (deactivated), never deleted, so this reference is stable'"
        ))


def downgrade():
    if _is_postgresql():
        op.execute(sa.text(
            "COMMENT ON COLUMN request_logs.model_endpoint_id IS "
            "'Backend endpoint that served the request; SET NULL on delete to preserve historical data'"
        ))
    op.drop_column("model_endpoints", "active")
