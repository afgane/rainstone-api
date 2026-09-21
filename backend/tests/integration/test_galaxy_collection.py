"""Galaxy extraction and collection against a Galaxy-shaped source schema.

The source tables below mirror the columns and relationships observed on the
AnVIL dev instance, including the nested invocation that expands through a
subworkflow association and an implicit collection.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from rainstone.adapters.contracts import ObservationBatch
from rainstone.adapters.galaxy_db import GalaxyDatabaseAdapter, discover_capabilities
from rainstone.baseline import BaselineProfile, classify_job
from rainstone.collector import Collector, ScheduledSource
from rainstone.config import Settings
from rainstone.costing import calculate_tenant
from rainstone.db import engine
from rainstone.ingestion import apply_batch, read_cursor, stable_id
from rainstone.models import (
    CostLine,
    ExecutionAttempt,
    IngestionState,
    Invocation,
    InvocationJob,
    Job,
    JobMetric,
    JobStateEvent,
    LifetimeAttempt,
    Owner,
    ResourceLifetime,
    Tenant,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

SCHEMA = "galaxy_src"
TENANT_SLUG = "collector-test"
TENANT_ID = stable_id("tenant", TENANT_SLUG)

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};

CREATE TABLE galaxy_user (id BIGINT PRIMARY KEY, username VARCHAR, email VARCHAR,
                          password VARCHAR);
CREATE TABLE history (id BIGINT PRIMARY KEY, user_id BIGINT);
CREATE TABLE job (
    id BIGINT PRIMARY KEY, create_time TIMESTAMP, update_time TIMESTAMP,
    user_id BIGINT, history_id BIGINT, tool_id VARCHAR, tool_version TEXT,
    state VARCHAR, exit_code INTEGER, handler VARCHAR, job_runner_name VARCHAR,
    job_runner_external_id VARCHAR, destination_id VARCHAR,
    copied_from_job_id BIGINT, command_line TEXT, destination_params BYTEA
);
CREATE TABLE job_state_history (id BIGSERIAL PRIMARY KEY, job_id BIGINT,
                                state VARCHAR, create_time TIMESTAMP);
CREATE TABLE job_metric_numeric (id BIGSERIAL PRIMARY KEY, job_id BIGINT,
                                 plugin VARCHAR, metric_name VARCHAR,
                                 metric_value NUMERIC);
CREATE TABLE workflow (id BIGINT PRIMARY KEY, stored_workflow_id BIGINT, name VARCHAR);
CREATE TABLE workflow_invocation (id BIGINT PRIMARY KEY, create_time TIMESTAMP,
                                  update_time TIMESTAMP, workflow_id BIGINT,
                                  history_id BIGINT, state VARCHAR);
CREATE TABLE workflow_invocation_step (
    id BIGINT PRIMARY KEY, create_time TIMESTAMP, update_time TIMESTAMP,
    workflow_invocation_id BIGINT, workflow_step_id BIGINT, state VARCHAR,
    job_id BIGINT, implicit_collection_jobs_id BIGINT
);
CREATE TABLE workflow_invocation_to_subworkflow_invocation_association (
    id BIGSERIAL PRIMARY KEY, workflow_invocation_id BIGINT,
    subworkflow_invocation_id BIGINT
);
CREATE TABLE implicit_collection_jobs_job_association (
    id BIGSERIAL PRIMARY KEY, implicit_collection_jobs_id BIGINT, job_id BIGINT,
    order_index INTEGER
);

INSERT INTO galaxy_user VALUES (1, 'researcher', 'hidden@example.invalid', 'hash'),
                               (2, 'other', 'other@example.invalid', 'hash');
INSERT INTO history VALUES (1, 1), (2, 2);

INSERT INTO job VALUES
 (1, '2026-09-21 11:55:42', '2026-09-21 11:56:29', 1, 1, 'upload1', '1.1.7', 'ok', 0,
  'job-handler-0', 'local', '568', 'local', NULL, 'rm -rf never-read', NULL),
 (2, '2026-09-21 12:00:00', '2026-09-21 12:10:00', 1, 1,
  'toolshed.g2.bx.psu.edu/repos/goeckslab/image_learner/image_learner/1.0.0', '1.0.0',
  'error', 1, 'job-handler-0', 'gcp_batch', 'galaxy-batch-demo-336', 'gcp_batch', NULL,
  'never-read', NULL),
 (3, '2026-09-21 12:20:00', '2026-09-21 12:20:05', 1, 1, 'cat1', '1.0.0', 'paused',
  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
 (45, '2026-09-21 12:30:00', '2026-09-21 12:31:00', 1, 1, 'cat1', '1.0.0', 'ok', 0,
  'job-handler-0', 'k8s', 'gxy-galaxy-aaaaa', 'k8s', NULL, 'never-read', NULL),
 (46, '2026-09-21 12:30:00', '2026-09-21 12:31:30', 1, 1, 'cat1', '1.0.0', 'ok', 0,
  'job-handler-0', 'k8s', 'gxy-galaxy-bbbbb', 'k8s', NULL, 'never-read', NULL),
 (60, '2026-09-21 12:40:00', '2026-09-21 12:41:00', 2, 2, 'upload1', '1.1.7', 'ok', 0,
  'job-handler-0', 'local', '600', 'local', NULL, 'never-read', NULL);

INSERT INTO job_state_history (job_id, state, create_time) VALUES
 (1, 'new', '2026-09-21 11:55:42'), (1, 'ok', '2026-09-21 11:56:29'),
 (3, 'new', '2026-09-21 12:20:00'), (3, 'paused', '2026-09-21 12:20:05');

INSERT INTO job_metric_numeric (job_id, plugin, metric_name, metric_value) VALUES
 (1, 'core', 'galaxy_slots', 1),
 (1, 'core', 'start_epoch', 1789991760),
 (1, 'core', 'end_epoch', 1789991820),
 (1, 'cgroup', 'cpu.stat.usage_usec', 14374096),
 (1, 'core', 'not_allowlisted', 99),
 (45, 'core', 'galaxy_slots', 1),
 (45, 'core', 'start_epoch', 1789993800),
 (45, 'core', 'end_epoch', 1789993860);

INSERT INTO workflow VALUES (10, 3, 'Nested collection demo'), (11, 3, 'Child workflow');
INSERT INTO workflow_invocation VALUES
 (4, '2026-09-21 12:29:00', '2026-09-21 12:31:40', 10, 1, 'scheduled'),
 (5, '2026-09-21 12:29:30', '2026-09-21 12:31:40', 11, 1, 'scheduled');
INSERT INTO workflow_invocation_to_subworkflow_invocation_association
 (workflow_invocation_id, subworkflow_invocation_id) VALUES (4, 5);
INSERT INTO workflow_invocation_step VALUES
 (20, '2026-09-21 12:29:00', '2026-09-21 12:29:00', 4, 20, 'scheduled', NULL, NULL),
 (21, '2026-09-21 12:29:30', '2026-09-21 12:31:40', 5, 21, 'scheduled', NULL, 1);
INSERT INTO implicit_collection_jobs_job_association
 (implicit_collection_jobs_id, job_id, order_index) VALUES (1, 45, 0), (1, 46, 1);
"""

PROFILE = BaselineProfile(
    version="collector-test-baseline",
    resource_uid="galaxy-baseline-vm",
    machine_type="t2d-standard-4",
    region="us-central1",
    zone="us-central1-a",
    destinations=("local",),
    runners=("local",),
)


@pytest.fixture(scope="module")
def source_engine():
    # The source runs in its own schema with its own connections, so the
    # application engine's search path is never changed.
    source = create_engine(
        engine.url,
        connect_args={"options": f"-csearch_path={SCHEMA},public"},
        pool_pre_ping=True,
    )
    with source.begin() as connection:
        connection.execute(text(DDL))
    # Start from a known collection state so the module does not depend on
    # cursors or rows left by an earlier run of these tests.
    with Session(engine) as session:
        tenant = session.get(Tenant, TENANT_ID)
        if tenant is not None:
            session.delete(tenant)
            session.commit()
        session.add(
            Tenant(
                id=TENANT_ID,
                slug=TENANT_SLUG,
                display_name="Collector integration test",
                capabilities={"demo": False},
            )
        )
        session.commit()
    yield source
    source.dispose()
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


def classify(batch: ObservationBatch) -> ObservationBatch:
    from dataclasses import replace

    return replace(batch, jobs=tuple(classify_job(job, PROFILE) for job in batch.jobs))


def drain(adapter: GalaxyDatabaseAdapter, *, limit: int = 10) -> list[dict]:
    """Collect until the adapter reports its current work exhausted."""
    results = []
    with Session(engine) as session:
        cursor = read_cursor(session, TENANT_ID, adapter.source_name)
    for _ in range(limit):
        batch = classify(adapter.collect(cursor))
        with Session(engine) as session:
            results.append(apply_batch(session, TENANT_ID, batch))
            session.commit()
            cursor = read_cursor(session, TENANT_ID, adapter.source_name)
        if batch.exhausted:
            break
    return results


def test_schema_capabilities_are_discovered(source_engine) -> None:
    capabilities = discover_capabilities(source_engine)
    assert capabilities.compatible
    assert capabilities.missing == ()
    assert len(capabilities.schema_fingerprint) == 64
    assert "job" in capabilities.details["tables"]


def test_backfill_then_incremental_collects_jobs_owners_metrics_and_states(source_engine) -> None:
    adapter = GalaxyDatabaseAdapter(source_engine, batch_size=2)
    results = drain(adapter)
    assert len(results) > 1  # paged rather than one unbounded read
    with Session(engine) as session:
        jobs = {
            job.source_id: job
            for job in session.scalars(select(Job).where(Job.tenant_id == TENANT_ID))
        }
        assert set(jobs) == {"1", "2", "3", "45", "46", "60"}
        assert jobs["2"].state == "error"
        assert jobs["2"].exit_code == 1
        assert jobs["1"].handler == "job-handler-0"
        owners = {
            owner.source_id: owner.label
            for owner in session.scalars(select(Owner).where(Owner.tenant_id == TENANT_ID))
        }
        assert owners == {"1": "researcher", "2": "other"}
        metrics = list(
            session.scalars(select(JobMetric).where(JobMetric.job_id == jobs["1"].id))
        )
        assert {metric.name for metric in metrics} == {
            "galaxy_slots", "start_epoch", "end_epoch", "cpu.stat.usage_usec",
        }
        events = list(
            session.scalars(select(JobStateEvent).where(JobStateEvent.job_id == jobs["1"].id))
        )
        assert {event.state for event in events} == {"new", "ok"}
        state = session.scalar(
            select(IngestionState).where(
                IngestionState.tenant_id == TENANT_ID, IngestionState.source == "galaxy_db"
            )
        )
        assert state.cursor["phase"] == "incremental"
        assert state.status == "healthy"


def test_nested_invocation_membership_matches_the_recursive_evidence(source_engine) -> None:
    drain(GalaxyDatabaseAdapter(source_engine))
    with Session(engine) as session:
        invocations = {
            invocation.source_id: invocation
            for invocation in session.scalars(
                select(Invocation).where(Invocation.tenant_id == TENANT_ID)
            )
        }
        assert set(invocations) == {"4", "5"}
        assert invocations["5"].parent_id == invocations["4"].id
        assert invocations["4"].workflow_family_id == "3"
        memberships = session.execute(
            select(InvocationJob.relationship, Job.source_id)
            .join(Job, InvocationJob.job_id == Job.id)
            .where(InvocationJob.invocation_id == invocations["5"].id)
        ).all()
        assert sorted(source for _, source in memberships) == ["45", "46"]
        assert {relationship for relationship, _ in memberships} == {"collection"}
        # Ownership comes from the invocation's history, not a user column.
        owner = session.get(Owner, invocations["4"].owner_id)
        assert owner.source_id == "1"


def test_baseline_local_work_is_known_zero_and_paused_work_has_no_interval(source_engine) -> None:
    drain(GalaxyDatabaseAdapter(source_engine))
    with Session(engine) as session:
        revision = calculate_tenant(session, TENANT_ID, reason="test")
        session.commit()
        jobs = {
            job.source_id: job
            for job in session.scalars(select(Job).where(Job.tenant_id == TENANT_ID))
        }
        lines = {
            (line.job_id, line.basis): line
            for line in session.scalars(
                select(CostLine).where(CostLine.revision_id == revision.id)
            )
        }
        local = lines[(jobs["1"].id, "additional")]
        assert local.amount == Decimal("0")
        assert local.quality.value == "known_zero"
        assert lines[(jobs["1"].id, "allocated")].amount is None
        assert (jobs["3"].id, "additional") not in lines
        assert not list(
            session.scalars(
                select(ResourceLifetime)
                .join(LifetimeAttempt, LifetimeAttempt.lifetime_id == ResourceLifetime.id)
                .join(ExecutionAttempt, LifetimeAttempt.attempt_id == ExecutionAttempt.id)
                .where(ExecutionAttempt.job_id == jobs["3"].id)
            )
        )


def test_replay_and_restart_do_not_duplicate_or_revalue(source_engine) -> None:
    adapter = GalaxyDatabaseAdapter(source_engine)
    drain(adapter)
    with Session(engine) as session:
        first = calculate_tenant(session, TENANT_ID, reason="test")
        session.commit()
        before = session.scalar(
            select(func.count()).select_from(CostLine).where(CostLine.revision_id == first.id)
        )
        lifetimes = session.scalar(
            select(func.count())
            .select_from(ResourceLifetime)
            .where(ResourceLifetime.tenant_id == TENANT_ID)
        )
    # A restarted collector re-reads its replay overlap and the reconciliation set.
    drain(GalaxyDatabaseAdapter(source_engine))
    with Session(engine) as session:
        second = calculate_tenant(session, TENANT_ID, reason="test")
        session.commit()
        assert second.id == first.id
        assert session.scalar(
            select(func.count()).select_from(CostLine).where(CostLine.revision_id == first.id)
        ) == before
        assert session.scalar(
            select(func.count())
            .select_from(ResourceLifetime)
            .where(ResourceLifetime.tenant_id == TENANT_ID)
        ) == lifetimes


def test_late_metrics_without_a_job_update_are_reconciled(source_engine) -> None:
    drain(GalaxyDatabaseAdapter(source_engine))
    with source_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job_metric_numeric (job_id, plugin, metric_name, metric_value) "
                "VALUES (46, 'core', 'start_epoch', 1789993800), "
                "(46, 'core', 'end_epoch', 1789993890)"
            )
        )
    adapter = GalaxyDatabaseAdapter(source_engine, batch_size=3)
    for _ in range(4):  # the bounded sweep revisits older jobs over several cycles
        drain(adapter)
    with Session(engine) as session:
        job = session.scalar(
            select(Job).where(Job.tenant_id == TENANT_ID, Job.source_id == "46")
        )
        metrics = {
            metric.name for metric in session.scalars(
                select(JobMetric).where(JobMetric.job_id == job.id)
            )
        }
        assert {"start_epoch", "end_epoch"} <= metrics


def test_late_collection_membership_is_recovered_after_the_jobs_arrive(source_engine) -> None:
    with source_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job VALUES (70, '2026-09-21 13:00:00', '2026-09-21 13:01:00', 1, 1, "
                "'cat1', '1.0.0', 'ok', 0, 'job-handler-0', 'k8s', 'gxy-late', 'k8s', NULL, "
                "'never-read', NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO implicit_collection_jobs_job_association "
                "(implicit_collection_jobs_id, job_id, order_index) VALUES (1, 70, 2)"
            )
        )
    drain(GalaxyDatabaseAdapter(source_engine))
    with Session(engine) as session:
        invocation = session.scalar(
            select(Invocation).where(Invocation.tenant_id == TENANT_ID, Invocation.source_id == "5")
        )
        sources = session.scalars(
            select(Job.source_id)
            .join(InvocationJob, InvocationJob.job_id == Job.id)
            .where(InvocationJob.invocation_id == invocation.id)
        ).all()
        assert sorted(sources) == ["45", "46", "70"]


def test_a_failing_source_records_status_without_advancing_the_cursor(source_engine) -> None:
    drain(GalaxyDatabaseAdapter(source_engine))
    with Session(engine) as session:
        before = read_cursor(session, TENANT_ID, "galaxy_db")

    class BrokenAdapter:
        source_name = "galaxy_db"

        def collect(self, cursor: dict) -> ObservationBatch:
            raise RuntimeError("source connection reset")

    settings = Settings(
        auth_mode="development", demo_data=True, tenant_slug=TENANT_SLUG,
        collect_max_backoff_seconds=4,
    )
    collector = Collector(
        settings,
        [ScheduledSource(adapter=BrokenAdapter(), interval_seconds=30)],
        engine=engine,
    )
    result = collector.collect_source(collector._sources[0])  # noqa: SLF001 - direct cycle
    assert result["status"] == "failed"
    assert result["retry_in_seconds"] > 0
    with Session(engine) as session:
        state = session.scalar(
            select(IngestionState).where(
                IngestionState.tenant_id == TENANT_ID, IngestionState.source == "galaxy_db"
            )
        )
        assert state.status == "failed"
        assert state.consecutive_failures == 1
        assert "RuntimeError" in state.error
        assert read_cursor(session, TENANT_ID, "galaxy_db") == before


def test_provider_retry_charges_one_shared_vm_lifetime(source_engine) -> None:
    """The saved CI same-VM retry must never produce two whole-VM charges."""
    import json
    from pathlib import Path

    from rainstone.adapters.gcp_batch import normalize_batch_job

    drain(GalaxyDatabaseAdapter(source_engine))
    payload = json.loads(Path("fixtures/contract/batch-job-retry.json").read_text())
    attempts = normalize_batch_job(
        payload,
        payload["tasks"],
        project="demo-project",
        location="us-east4",
        job_source_id="2",
    )
    batch = ObservationBatch(
        source="gcp_batch",
        observed_at=datetime.now(UTC),
        attempts=tuple(attempts),
        cursor={"offset": 0},
    )
    with Session(engine) as session:
        result = apply_batch(session, TENANT_ID, batch)
        session.commit()
        revision = calculate_tenant(session, TENANT_ID, reason="test")
        session.commit()
        assert result["attempts"] == 2
        job = session.scalar(
            select(Job).where(Job.tenant_id == TENANT_ID, Job.source_id == "2")
        )
        lifetimes = {
            lifetime.id: lifetime
            for lifetime in session.scalars(
                select(ResourceLifetime)
                .join(LifetimeAttempt, LifetimeAttempt.lifetime_id == ResourceLifetime.id)
                .join(ExecutionAttempt, LifetimeAttempt.attempt_id == ExecutionAttempt.id)
                .where(ExecutionAttempt.job_id == job.id)
            )
        }
        assert len(lifetimes) == 1
        lifetimes = list(lifetimes.values())
        links = session.scalar(
            select(func.count())
            .select_from(LifetimeAttempt)
            .where(LifetimeAttempt.lifetime_id == lifetimes[0].id)
        )
        assert links == 2
        lines = list(
            session.scalars(
                select(CostLine).where(
                    CostLine.revision_id == revision.id,
                    CostLine.job_id == job.id,
                    CostLine.basis == "additional",
                )
            )
        )
        # One line for the shared lifetime, not one per attempt.
        assert len(lines) == 1
        assert lines[0].attempt_id is None
        assert lines[0].details["shared_attempt_count"] == 2
        # us-east4 is outside the bundled catalog, so it stays visibly unpriced
        # rather than borrowing another region's rate.
        assert lines[0].amount is None
        assert lines[0].quality.value == "unpriced"
        attempts = list(
            session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.job_id == job.id))
        )
        provider_attempts = [
            attempt for attempt in attempts if attempt.source_attempt_id.startswith("batch:")
        ]
        assert len(provider_attempts) == 2
        assert {attempt.provider_outcome for attempt in provider_attempts} == {"SUCCEEDED"}
        assert any(attempt.exit_code == 125 for attempt in provider_attempts)
        assert job.state == "error"


def test_source_binding_refuses_a_replaced_source_database(source_engine) -> None:
    from rainstone.bootstrap import seed_instance
    from rainstone.collector import SourceBindingMismatch, ensure_binding

    settings = Settings(
        auth_mode="development", demo_data=True, tenant_slug=f"binding-{uuid.uuid4().hex[:8]}"
    )
    capabilities = discover_capabilities(source_engine)
    with Session(engine) as session:
        seed_instance(
            session,
            settings,
            capabilities,
            owner_source_id="1",
            owner_label="researcher",
            descriptor={"machine_type": "t2d-standard-4"},
        )
        ensure_binding(session, settings, capabilities.schema_fingerprint, capabilities.source_version)
        with pytest.raises(SourceBindingMismatch):
            ensure_binding(session, settings, "a-different-schema-fingerprint", None)


def test_provisioned_reader_role_has_least_privilege_reads(source_engine) -> None:
    """The scoped role reads allowlisted columns and nothing else."""
    from urllib.parse import quote

    from rainstone.bootstrap import provision_reader, reader_dsn

    role = "rainstone_reader_test"
    password = "aaaaBBBB1111-_test"

    def remove_role() -> None:
        with engine.begin() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            ).scalar()
            if exists:
                connection.execute(text(f'DROP OWNED BY "{role}" CASCADE'))
                connection.execute(text(f'DROP ROLE "{role}"'))

    remove_role()
    try:
        with source_engine.begin() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
            provision_reader(
                connection,
                role=role,
                password=password,
                database=database,
                schema=SCHEMA,
                rotate=True,
            )
        dsn = reader_dsn(str(engine.url.render_as_string(hide_password=False)), role, password)
        assert quote(password, safe="") in dsn
        reader = create_engine(
            dsn, connect_args={"options": f"-csearch_path={SCHEMA}"}, poolclass=None
        )
        with reader.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM job")).scalar() > 0
            # The late-membership test adds a third mapped job earlier in this
            # module, so only the presence of the association matters here.
            assert connection.execute(
                text("SELECT count(*) FROM implicit_collection_jobs_job_association")
            ).scalar() >= 2
            for forbidden in (
                "SELECT command_line FROM job LIMIT 1",
                "SELECT password FROM galaxy_user LIMIT 1",
                "SELECT destination_params FROM job LIMIT 1",
            ):
                with pytest.raises(Exception, match="permission denied"):
                    connection.execute(text(forbidden))
                connection.rollback()
            with pytest.raises(Exception, match="permission denied"):
                connection.execute(text("UPDATE job SET state = 'ok' WHERE id = 1"))
            connection.rollback()
        reader.dispose()
    finally:
        remove_role()
