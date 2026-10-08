"""Phase 9 runtime budget, task commands, and Task Knowledge Graph metadata.

Revision ID: 0005_phase9_runtime_kg
Revises: 0004_phase5_memory
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import (
    ModelPricingRow,
    ResearchTaskCommandRow,
    TaskKnowledgeGraphRow,
)

revision: str = "0005_phase9_runtime_kg"
down_revision: Union[str, None] = "0004_phase5_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE9_TABLES = (
    ModelPricingRow.__table__,
    ResearchTaskCommandRow.__table__,
    TaskKnowledgeGraphRow.__table__,
)


def upgrade() -> None:
    # Revision 0001 creates current metadata for fresh/offline installs. Existing
    # databases reach this revision without the Phase 9 control tables.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE9_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE9_TABLES):
        table.drop(bind=bind, checkfirst=True)
