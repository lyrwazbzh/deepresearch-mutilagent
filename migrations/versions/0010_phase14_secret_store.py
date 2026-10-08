"""Phase 14 close-out Secret Store.

Revision ID: 0010_phase14_secret_store
Revises: 0009_phase14_share_export
"""

from typing import Sequence, Union

from alembic import context, op

from src.agent.backends.persistence.models import SecretRow

revision: str = "0010_phase14_secret_store"
down_revision: Union[str, None] = "0009_phase14_share_export"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    SecretRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    SecretRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
