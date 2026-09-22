"""Record when a release's initialization completed.

Revision ID: 0006
Revises: 0005

Readiness checks schema and account state, which an upgrade that changes no
migration already satisfies before its own initialization runs. A per-release
marker closes that window.
"""

from alembic import op

from rainstone import models  # noqa: F401
from rainstone.db import Base

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS installation_record")
