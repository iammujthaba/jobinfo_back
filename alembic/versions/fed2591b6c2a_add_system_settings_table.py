"""add system_settings table

Revision ID: fed2591b6c2a
Revises: 71e31ee9371a
Create Date: 2026-09-13 09:29:47.235199

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fed2591b6c2a'
down_revision: Union[str, None] = '71e31ee9371a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    if 'system_settings' not in tables:
        op.create_table(
            'system_settings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('key', sa.String(length=50), nullable=False),
            sa.Column('value', sa.String(length=255), nullable=False),
            sa.Column('description', sa.String(length=255), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('system_settings', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_system_settings_id'), ['id'], unique=False)
            batch_op.create_index(batch_op.f('ix_system_settings_key'), ['key'], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    if 'system_settings' in tables:
        with op.batch_alter_table('system_settings', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_system_settings_key'))
            batch_op.drop_index(batch_op.f('ix_system_settings_id'))
        op.drop_table('system_settings')
