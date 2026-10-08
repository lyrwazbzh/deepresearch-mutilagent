"""Phase 12 observability and evaluation persistence.

Revision ID: 0007_phase12_obs_eval
Revises: 0006_phase10_http_auth
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import EvalCaseResultRow, EvalRunRow, ObservabilitySpanRow

revision: str = "0007_phase12_obs_eval"
down_revision: Union[str, None] = "0006_phase10_http_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE12_TABLES = (
    ObservabilitySpanRow.__table__,
    EvalRunRow.__table__,
    EvalCaseResultRow.__table__,
)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE12_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE12_TABLES):
        table.drop(bind=bind, checkfirst=True)
