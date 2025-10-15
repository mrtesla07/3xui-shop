"""add payment uuid to transactions

Revision ID: 1d5d6e69d5c6
Revises: 032f2bef8d8d
Create Date: 2025-10-15 16:15:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d5d6e69d5c6"
down_revision: Union[str, None] = "032f2bef8d8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("payment_uuid", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_transactions_payment_uuid",
        "transactions",
        ["payment_uuid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_transactions_payment_uuid",
        "transactions",
        type_="unique",
    )
    op.drop_column("transactions", "payment_uuid")
