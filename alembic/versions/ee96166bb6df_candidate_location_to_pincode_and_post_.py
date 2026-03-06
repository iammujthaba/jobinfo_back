"""candidate_location_to_pincode_and_post_office

Revision ID: ee96166bb6df
Revises: 7fccd863318f
Create Date: 2026-03-05 17:33:07.573746

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee96166bb6df'
down_revision: Union[str, None] = '7fccd863318f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('candidate_table', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pin_code', sa.String(length=6), nullable=True))
        batch_op.add_column(sa.Column('post_office', sa.String(length=200), nullable=True))
        batch_op.drop_column('location')


def downgrade() -> None:
    with op.batch_alter_table('candidate_table', schema=None) as batch_op:
        batch_op.add_column(sa.Column('location', sa.VARCHAR(length=200), nullable=True))
        batch_op.drop_column('post_office')
        batch_op.drop_column('pin_code')
