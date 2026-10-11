"""per-run consent and public gallery card

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('consent', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('consent_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('public_title', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('public_place', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('public_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('public_summary', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('share_hash', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_runs_public_at'), ['public_at'], unique=False)

def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_public_at'))
        for col in ('share_hash', 'public_summary', 'public_at', 'public_place',
                    'public_title', 'consent_at', 'consent'):
            batch_op.drop_column(col)
