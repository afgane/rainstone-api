"""Initial durable reporting foundation.

Revision ID: 0001
Revises:
"""
from alembic import op

from rainstone.db import Base
from rainstone import models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
