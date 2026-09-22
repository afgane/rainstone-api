"""The reporting schema.

Revision ID: 0001
Revises:

One revision creates the whole schema. Rainstone has not been deployed, so
there is no installed database whose history these revisions would have to
preserve; carrying the development cycle's steps forward would only make the
first real installation replay work that never ran anywhere.

Report-affecting writes advance a per-tenant marker through statement-level
triggers, so a write that bypasses the application still invalidates pinned
report snapshots.
"""

from rainstone import models  # noqa: F401
from rainstone.db import Base

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Each table names the query that resolves its changed rows to tenants. The
# transition table is always called `changed`. Every query joins `tenant`, so a
# tenant removal and its cascaded child deletes do not try to mark a tenant that
# no longer exists. Price changes affect every tenant, because catalogs are
# shared.
_DIRECT = "SELECT c.tenant_id FROM changed c JOIN tenant t ON t.id = c.tenant_id"

TENANT_SOURCES: dict[str, str] = {
    "tenant": "SELECT c.id AS tenant_id FROM changed c JOIN tenant t ON t.id = c.id",
    "owner": _DIRECT,
    "job": _DIRECT,
    "execution_attempt": (
        "SELECT j.tenant_id FROM changed c JOIN job j ON j.id = c.job_id "
        "JOIN tenant t ON t.id = j.tenant_id"
    ),
    "resource_lifetime": _DIRECT,
    "resource_segment": (
        "SELECT l.tenant_id FROM changed c "
        "JOIN resource_lifetime l ON l.id = c.lifetime_id "
        "JOIN tenant t ON t.id = l.tenant_id"
    ),
    "lifetime_attempt": (
        "SELECT l.tenant_id FROM changed c "
        "JOIN resource_lifetime l ON l.id = c.lifetime_id "
        "JOIN tenant t ON t.id = l.tenant_id"
    ),
    "invocation": _DIRECT,
    "invocation_job": (
        "SELECT i.tenant_id FROM changed c "
        "JOIN invocation i ON i.id = c.invocation_id "
        "JOIN tenant t ON t.id = i.tenant_id"
    ),
    "deployment_policy": _DIRECT,
    "infrastructure_interval": _DIRECT,
    "price_version": "SELECT id AS tenant_id FROM tenant WHERE EXISTS (SELECT 1 FROM changed)",
}

OPERATIONS = (("insert", "INSERT", "NEW"), ("update", "UPDATE", "NEW"), ("delete", "DELETE", "OLD"))

FUNCTION = """
CREATE OR REPLACE FUNCTION rainstone_advance_report_generation()
RETURNS trigger AS $$
BEGIN
    EXECUTE format(
        'INSERT INTO report_generation (tenant_id, generation, updated_at) '
        'SELECT DISTINCT x.tenant_id, 1, now() FROM (%s) x '
        'WHERE x.tenant_id IS NOT NULL '
        'ON CONFLICT (tenant_id) DO UPDATE '
        '   SET generation = report_generation.generation + 1, updated_at = now()',
        TG_ARGV[0]);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    op.execute(FUNCTION)
    for table, source in TENANT_SOURCES.items():
        for suffix, operation, transition in OPERATIONS:
            name = f"rainstone_generation_{table}_{suffix}"
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            op.execute(f"""
                CREATE TRIGGER {name}
                AFTER {operation} ON {table}
                REFERENCING {transition} TABLE AS changed
                FOR EACH STATEMENT
                EXECUTE FUNCTION rainstone_advance_report_generation('{source}')
            """)


def downgrade() -> None:
    for table in TENANT_SOURCES:
        for suffix, _, _ in OPERATIONS:
            op.execute(f"DROP TRIGGER IF EXISTS rainstone_generation_{table}_{suffix} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS rainstone_advance_report_generation()")
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
