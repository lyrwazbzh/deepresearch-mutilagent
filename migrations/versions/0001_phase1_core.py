"""Phase 1 core tables.

Revision ID: 0001_phase1_core
Revises:
"""
from typing import Sequence, Union

from alembic import op

from src.agent.backends.persistence.models import Base

revision: str = "0001_phase1_core"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

