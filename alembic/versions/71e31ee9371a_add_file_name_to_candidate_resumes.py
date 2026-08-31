"""add file_name to candidate_resumes

Revises: c2a8e8f81234
Create Date: 2026-08-31 23:35:09.314094
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '71e31ee9371a'
down_revision: Union[str, None] = 'c2a8e8f81234'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('candidate_resumes', sa.Column('file_name', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('candidate_resumes', 'file_name')