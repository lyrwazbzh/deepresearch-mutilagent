"""Phase 10 HTTP authentication and tenant membership.

Revision ID: 0006_phase10_http_auth
Revises: 0005_phase9_runtime_kg
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import (
    AuthSessionRow,
    TenantRow,
    TenantUserMembershipRow,
    UserRow,
)

revision: str = "0006_phase10_http_auth"
down_revision: Union[str, None] = "0005_phase9_runtime_kg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PHASE10_TABLES = (
    TenantRow.__table__,
    UserRow.__table__,
    TenantUserMembershipRow.__table__,
    AuthSessionRow.__table__,
)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in PHASE10_TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    for table in reversed(PHASE10_TABLES):
        table.drop(bind=bind, checkfirst=True)
