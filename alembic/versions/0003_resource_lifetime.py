"""Separate chargeable resource lifetimes from their attempt associations.

Revision ID: 0003
Revises: 0002

A retry can reuse one VM, as CI job 336 demonstrates, so a lifetime is stored
and priced once and associated with every attempt that used it. Existing
`resource_interval` rows are migrated into lifetimes, one segment each, and an
association to their attempt.

Derived cost rows are not migrated: this release also changes the calculation
version, so revisions are recomputed from the retained source facts on the next
collection rather than being relabelled.
"""

from alembic import op
from sqlalchemy import inspect

from rainstone import models  # noqa: F401
from rainstone.db import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ADDED_COLUMNS = (
    ("job", "exit_code", "INTEGER"),
    ("job", "handler", "VARCHAR(200)"),
    ("job", "resource_hints", "JSON DEFAULT '{}'::json"),
    ("invocation", "workflow_family_id", "VARCHAR(300)"),
    ("invocation", "membership_settled", "BOOLEAN DEFAULT FALSE"),
    ("execution_attempt", "provider_outcome", "VARCHAR(40)"),
    ("execution_attempt", "exit_code", "INTEGER"),
    ("execution_attempt", "task_index", "INTEGER"),
    ("execution_attempt", "attempt_ordinal", "INTEGER"),
    ("execution_attempt", "facts", "JSON DEFAULT '{}'::json"),
    ("ingestion_state", "last_attempt_at", "TIMESTAMP WITH TIME ZONE"),
    ("ingestion_state", "consecutive_failures", "INTEGER DEFAULT 0"),
    ("ingestion_state", "metrics", "JSON DEFAULT '{}'::json"),
    ("cost_line", "lifetime_id", "UUID"),
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    for table, column, definition in ADDED_COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
    op.execute("ALTER TABLE execution_attempt ALTER COLUMN runner DROP NOT NULL")
    op.execute("ALTER TABLE cost_line ALTER COLUMN attempt_id DROP NOT NULL")

    tables = set(inspect(bind).get_table_names())
    if "resource_interval" in tables:
        op.execute("""
            INSERT INTO resource_lifetime (
                id, tenant_id, provider, resource_key, resource_uid, region,
                machine_type, purchase_model, capacity_relationship,
                observed_start, observed_end, timing_method, requested_vcpu,
                requested_memory_mib, facts
            )
            SELECT DISTINCT ON (job.tenant_id, interval.provider, interval.source_interval_id)
                   interval.id, job.tenant_id, interval.provider,
                   interval.source_interval_id, interval.resource_uid, interval.region,
                   interval.machine_type, interval.purchase_model,
                   interval.capacity_relationship, interval.observed_start,
                   interval.observed_end, interval.timing_method, interval.requested_vcpu,
                   interval.requested_memory_mib,
                   coalesce(interval.facts, '{}'::json)
              FROM resource_interval interval
              JOIN execution_attempt attempt ON attempt.id = interval.attempt_id
              JOIN job ON job.id = attempt.job_id
             ORDER BY job.tenant_id, interval.provider, interval.source_interval_id, interval.id
            ON CONFLICT DO NOTHING
        """)
        op.execute("""
            INSERT INTO resource_segment (
                id, lifetime_id, source_segment_id, observed_start, observed_end,
                timing_method, facts
            )
            SELECT lifetime.id, lifetime.id, 'lifetime', lifetime.observed_start,
                   lifetime.observed_end, lifetime.timing_method, '{}'::json
              FROM resource_lifetime lifetime
            ON CONFLICT DO NOTHING
        """)
        op.execute("""
            INSERT INTO lifetime_attempt (
                lifetime_id, attempt_id, task_index, attempt_ordinal, correlation, facts
            )
            SELECT lifetime.id, interval.attempt_id, NULL, NULL, 'migrated', '{}'::json
              FROM resource_interval interval
              JOIN execution_attempt attempt ON attempt.id = interval.attempt_id
              JOIN job ON job.id = attempt.job_id
              JOIN resource_lifetime lifetime
                ON lifetime.tenant_id = job.tenant_id
               AND lifetime.provider = interval.provider
               AND lifetime.resource_key = interval.source_interval_id
            ON CONFLICT DO NOTHING
        """)
        op.execute("DELETE FROM cost_line")
        op.execute("DELETE FROM cost_revision")
        op.execute("ALTER TABLE cost_line DROP COLUMN IF EXISTS resource_interval_id")
        op.execute("DROP TABLE resource_interval")

    op.execute("ALTER TABLE cost_line DROP CONSTRAINT IF EXISTS uq_cost_line_revision_interval")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_cost_line_lifetime_job'
            ) THEN
                ALTER TABLE cost_line
                  ADD CONSTRAINT uq_cost_line_lifetime_job
                  UNIQUE (revision_id, lifetime_id, job_id, basis, component);
            END IF;
        END $$
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'cost_line_lifetime_id_fkey'
            ) THEN
                ALTER TABLE cost_line
                  ADD CONSTRAINT cost_line_lifetime_id_fkey
                  FOREIGN KEY (lifetime_id) REFERENCES resource_lifetime (id) ON DELETE CASCADE;
            END IF;
        END $$
    """)


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade would discard resource lifetime and attempt associations; "
        "restore from backup instead."
    )
