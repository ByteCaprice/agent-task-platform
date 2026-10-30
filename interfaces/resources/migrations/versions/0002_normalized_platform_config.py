"""Add normalized, database-managed platform configuration.

Revision ID: 0002_normalized_platform_config
Revises: 0001_platform_baseline
"""

from collections.abc import Sequence

from alembic import op

from infra.store.model import AiApiKeyConfig, AiCallbackConfig, AiModelConfig, AiRuntimeConfig

revision: str = "0002_normalized_platform_config"
down_revision: str | None = "0001_platform_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    AiRuntimeConfig.__table__,
    AiModelConfig.__table__,
    AiCallbackConfig.__table__,
    AiApiKeyConfig.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
