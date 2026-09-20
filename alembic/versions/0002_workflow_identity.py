"""Add a stable workflow identity for reporting.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invocation ADD COLUMN IF NOT EXISTS workflow_id VARCHAR(300)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_workflow_identity "
        "ON invocation (tenant_id, workflow_id, workflow_version)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_invocation_workflow_identity")
    op.execute("ALTER TABLE invocation DROP COLUMN IF EXISTS workflow_id")
