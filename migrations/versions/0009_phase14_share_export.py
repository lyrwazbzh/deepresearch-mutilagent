"""Phase 14 Share and Export control records.

Revision ID: 0009_phase14_share_export
Revises: 0008_phase13_tool_flywheel
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import ExportJobRow, ShareRow

revision: str = "0009_phase14_share_export"
down_revision: Union[str, None] = "0008_phase13_tool_flywheel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE14_TABLES = (ShareRow.__table__, ExportJobRow.__table__)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE14_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE14_TABLES):
        table.drop(bind=bind, checkfirst=True)
