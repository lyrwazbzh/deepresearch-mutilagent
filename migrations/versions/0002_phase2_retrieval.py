"""Phase 2 retrieval, workspace, document and datasource tables.

Revision ID: 0002_phase2_retrieval
Revises: 0001_phase1_core
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from src.agent.backends.persistence.models import (
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    DatasetCatalogRow,
    DocumentChunkRow,
    DocumentIngestionRow,
    DocumentRow,
    DocumentUploadReservationRow,
    WorkspaceFileRow,
)

revision: str = "0002_phase2_retrieval"
down_revision: Union[str, None] = "0001_phase1_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE2_TABLES = (
    WorkspaceFileRow.__table__,
    DocumentRow.__table__,
    DocumentIngestionRow.__table__,
    DocumentChunkRow.__table__,
    DocumentUploadReservationRow.__table__,
    DataSourceRow.__table__,
    DataSourceUserGrantRow.__table__,
    DataSourceTablePolicyRow.__table__,
    DatasetCatalogRow.__table__,
)


def upgrade() -> None:
    # Revision 0001 uses current Base.metadata for fresh installs, so all current
    # tables are already emitted when Alembic generates offline SQL.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("research_tasks")}
    if "capability_snapshot" not in columns:
        op.add_column(
            "research_tasks",
            sa.Column(
                "capability_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        op.alter_column("research_tasks", "capability_snapshot", server_default=None)
    for table in PHASE2_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE2_TABLES):
        table.drop(bind=bind, checkfirst=True)
    op.drop_column("research_tasks", "capability_snapshot")
