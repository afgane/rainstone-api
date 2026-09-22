"""Bind instances by enrolled source identity, not by schema visibility.

Revision ID: 0005
Revises: 0004

A schema hash is a capability of the *connection*: column-level grants make an
administrator and a restricted reader disagree about the same database, while
two unrelated databases with the same schema agree. Identity therefore moves to
an identifier enrolled inside the source database, and the schema fingerprint is
kept only as diagnostic capability information.

Existing bindings have no enrolled identity, so they are marked for
re-enrolment: `rainstone bootstrap` records the identity and rebinds.
"""

from alembic import op
from sqlalchemy import text

from rainstone import models  # noqa: F401
from rainstone.db import Base

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

PENDING = "pending-enrolment"


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE source_binding ADD COLUMN IF NOT EXISTS source_identity VARCHAR(64)")
    op.execute("ALTER TABLE source_binding ADD COLUMN IF NOT EXISTS schema_fingerprint VARCHAR(200)")
    op.execute(
        "ALTER TABLE source_binding ADD COLUMN IF NOT EXISTS enrolled_at TIMESTAMP WITH TIME ZONE"
    )
    # A statement is parsed whole, so the legacy column is only referenced when
    # it exists: a fresh database never had it.
    legacy = op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'source_binding' AND column_name = 'source_fingerprint'"
        )
    ).scalar()
    if legacy:
        op.execute(
            "UPDATE source_binding SET schema_fingerprint = source_fingerprint "
            "WHERE schema_fingerprint IS NULL"
        )
    op.execute(
        f"UPDATE source_binding SET source_identity = '{PENDING}' WHERE source_identity IS NULL"
    )
    op.execute("ALTER TABLE source_binding ALTER COLUMN source_identity SET NOT NULL")
    op.execute("ALTER TABLE source_binding DROP COLUMN IF EXISTS source_fingerprint")
    op.execute(
        "ALTER TABLE catalog_version ADD COLUMN IF NOT EXISTS signature_verified BOOLEAN DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE source_binding ADD COLUMN IF NOT EXISTS source_fingerprint VARCHAR(200)")
    op.execute("UPDATE source_binding SET source_fingerprint = schema_fingerprint")
    op.execute("ALTER TABLE source_binding DROP COLUMN IF EXISTS source_identity")
    op.execute("ALTER TABLE source_binding DROP COLUMN IF EXISTS schema_fingerprint")
    op.execute("ALTER TABLE source_binding DROP COLUMN IF EXISTS enrolled_at")
    op.execute("ALTER TABLE catalog_version DROP COLUMN IF EXISTS signature_verified")
    op.execute("DROP TABLE IF EXISTS capability_report")
