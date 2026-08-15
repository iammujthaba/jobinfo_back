"""add registrant_role to recruiter

Revision ID: c2a8e8f81234
Revises: 7708f84c4cff
Create Date: 2026-08-15 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2a8e8f81234'
down_revision: Union[str, None] = '7708f84c4cff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'recruiter_table',
        sa.Column('registrant_role', sa.String(length=50), nullable=False, server_default='other')
    )


def downgrade() -> None:
    op.drop_column('recruiter_table', 'registrant_role')
