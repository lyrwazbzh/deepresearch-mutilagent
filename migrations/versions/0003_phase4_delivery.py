"""Phase 4 controlled database writes and audit trail.

Revision ID: 0003_phase4_delivery
Revises: 0002_phase2_retrieval
"""
from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import AuditLogRow, PendingWriteRow

revision: str = "0003_phase4_delivery"
down_revision: Union[str, None] = "0002_phase2_retrieval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE4_TABLES = (PendingWriteRow.__table__, AuditLogRow.__table__)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE4_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE4_TABLES):
        table.drop(bind=bind, checkfirst=True)
