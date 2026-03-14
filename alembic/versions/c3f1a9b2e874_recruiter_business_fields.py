"""Recruiter business fields

Revision ID: c3f1a9b2e874
Revises: 7358fe0094ea
Create Date: 2026-03-14 12:48:00.000000

Drops the old personal-detail columns (name, email, company) from recruiter_table
and adds the new business-entity columns (company_name, business_type, business_contact).
The location column is kept but its length is tightened to 50 characters.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3f1a9b2e874"
down_revision: Union[str, None] = "7358fe0094ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    with op.batch_alter_table("recruiter_table") as batch_op:
        batch_op.drop_column("company_name")
        batch_op.drop_column("business_type")
        batch_op.drop_column("business_contact")

        batch_op.add_column(sa.Column("name",    sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("company", sa.String(200), nullable=True))
        batch_op.add_column(sa.Column("email",   sa.String(200), nullable=True))

        batch_op.alter_column("location", type_=sa.String(200))