"""Create the public platform baseline schema.

Revision ID: 0001_platform_baseline
Revises:
"""

from collections.abc import Sequence

from alembic import op

from infra.store.tables import metadata

revision: str = "0001_platform_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
