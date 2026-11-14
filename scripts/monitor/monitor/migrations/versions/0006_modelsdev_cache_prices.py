"""models.dev cache prices

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11

Daily models.dev rows gain the median, p5 and p95 of the providers' non-zero
cache-read and cache-write prices. They back the models.dev band of the
cached-input and cache-write price charts; days from before this revision, and
models no provider lists a cache price for, keep NULLs.

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

COLUMNS = (
    ('cache_read_median', 'Median provider cache-read price, $/M tokens'),
    ('cache_read_p5', '5th percentile provider cache-read price, $/M tokens'),
    ('cache_read_p95', '95th percentile provider cache-read price, $/M tokens'),
    ('cache_write_median', 'Median provider cache-write price, $/M tokens'),
    ('cache_write_p5', '5th percentile provider cache-write price, $/M tokens'),
    ('cache_write_p95', '95th percentile provider cache-write price, $/M tokens'),
)


def upgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        for name, comment in COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Float(), nullable=True, comment=comment))


def downgrade():
    with op.batch_alter_table('daily_stats') as batch_op:
        for name, _ in reversed(COLUMNS):
            batch_op.drop_column(name)
