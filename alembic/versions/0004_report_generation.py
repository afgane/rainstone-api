"""Track a report-generation marker maintained by the database.

Revision ID: 0004
Revises: 0003

Pinned report snapshots must be rejected whenever report-affecting facts
change, including a write that bypasses the application. Statement-level
triggers advance one marker per affected tenant, so each request compares a
single value instead of hashing every fact row.
"""

from alembic import op

from rainstone import models  # noqa: F401
from rainstone.db import Base

revision = "0004"
down_revision = "0003"
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
    op.execute(
        "ALTER TABLE cost_revision ADD COLUMN IF NOT EXISTS facts_generation BIGINT DEFAULT 0"
    )
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
        # A pre-0004 trigger name from an earlier draft, if present.
        op.execute(f"DROP TRIGGER IF EXISTS rainstone_generation_{table} ON {table}")
    op.execute("""
        INSERT INTO report_generation (tenant_id, generation, updated_at)
        SELECT id, 1, now() FROM tenant
        ON CONFLICT (tenant_id) DO NOTHING
    """)
    # Existing revisions adopt the current marker: their facts are unchanged by
    # this migration.
    op.execute("""
        UPDATE cost_revision r
           SET facts_generation = g.generation
          FROM report_generation g
         WHERE g.tenant_id = r.tenant_id
    """)


def downgrade() -> None:
    for table in TENANT_SOURCES:
        for suffix, _operation, _transition in OPERATIONS:
            op.execute(f"DROP TRIGGER IF EXISTS rainstone_generation_{table}_{suffix} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS rainstone_advance_report_generation()")
    op.execute("ALTER TABLE cost_revision DROP COLUMN IF EXISTS facts_generation")
    op.execute("DROP TABLE IF EXISTS report_generation")
