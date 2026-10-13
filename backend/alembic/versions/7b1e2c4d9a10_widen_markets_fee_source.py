"""widen markets.fee_source to fit 'exchange_public_schedule'

Revision ID: 7b1e2c4d9a10
Revises: 343294193a66
Create Date: 2026-10-13 10:55:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b1e2c4d9a10'
down_revision: Union[str, Sequence[str], None] = '343294193a66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('markets', 'fee_source', existing_type=sa.String(length=16), type_=sa.String(length=32),
                    existing_nullable=False)


def downgrade() -> None:
    op.alter_column('markets', 'fee_source', existing_type=sa.String(length=32), type_=sa.String(length=16),
                    existing_nullable=False)
