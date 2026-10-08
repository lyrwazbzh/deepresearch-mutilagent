"""Phase 13 Tool Decision Flywheel persistence.

Revision ID: 0008_phase13_tool_flywheel
Revises: 0007_phase12_obs_eval
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import DecisionTraceRow, ToolDecisionReviewRow

revision: str = "0008_phase13_tool_flywheel"
down_revision: Union[str, None] = "0007_phase12_obs_eval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE13_TABLES = (
    DecisionTraceRow.__table__,
    ToolDecisionReviewRow.__table__,
)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE13_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE13_TABLES):
        table.drop(bind=bind, checkfirst=True)
