"""Phase 5 Research LTM with pgvector and scoped uniqueness.

Revision ID: 0004_phase5_memory
Revises: 0003_phase4_delivery
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import ResearchMemoryRow

revision: str = "0004_phase5_memory"
down_revision: Union[str, None] = "0003_phase4_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Revision 0001 creates current metadata for fresh/offline installs. Existing
    # databases reach this revision with no research_memories table yet.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    ResearchMemoryRow.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    ResearchMemoryRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
