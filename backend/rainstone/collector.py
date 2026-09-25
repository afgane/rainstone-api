"""The unattended collector.

One collector runs per enrolled Galaxy instance, holding a PostgreSQL advisory
lease so a second process cannot double-collect. Each cycle collects one bounded
batch per due source, commits it before advancing that source's cursor, and then
recalculates costs. Failures record a visible status and back off with jitter;
they never advance a cursor.

No broker or separate orchestration platform is required, and web availability
is independent of this process.
"""

import logging
import random
import signal
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from rainstone.adapters.contracts import ObservationBatch, SourceAdapter
from rainstone.adapters.galaxy_db import GalaxyDatabaseAdapter, discover_capabilities
from rainstone.adapters.gcp_batch import BatchCollector, BatchTarget, HttpGcpClient
from rainstone.adapters.kubernetes import HttpKubernetesClient, KubernetesCollector
from rainstone.baseline import BaselineProfile, classify_job, saved_policy_conflict
from rainstone.catalog import parse_trusted_keys
from rainstone.catalog import refresh as refresh_catalog
from rainstone.config import Settings, get_settings
from rainstone.costing import calculate_tenant
from rainstone.db import engine as application_engine
from rainstone.doctor import record_report, run_checks
from rainstone.enrollment import read_evidence, source_engine, verify_enrollment
from rainstone.ingestion import (
    apply_batch,
    read_cursor,
    reclassify_baseline,
    record_failure,
    stable_id,
)
from rainstone.models import DeploymentPolicy, ExecutionAttempt, Job, Tenant

logger = logging.getLogger("rainstone.collector")

TERMINAL_STATES = {"ok", "error", "deleted", "deleting", "failed", "cancelled"}


class CollectorLeaseUnavailable(RuntimeError):
    """Another collector already holds this instance's lease."""


class BaselinePolicyConflict(RuntimeError):
    """The configured baseline period disagrees with the one saved for its version."""


def _lock_key(key: str) -> int:
    return int.from_bytes(uuid.uuid5(uuid.NAMESPACE_OID, key).bytes[:8], "big", signed=True)


class CollectorLease:
    """A session-scoped PostgreSQL advisory lock held for the process lifetime."""

    def __init__(self, engine: Engine, key: str) -> None:
        self._engine = engine
        self._key = _lock_key(key)
        self._connection = None

    def __enter__(self) -> "CollectorLease":
        self._connection = self._engine.connect()
        acquired = self._connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
        ).scalar()
        if not acquired:
            self._connection.close()
            self._connection = None
            raise CollectorLeaseUnavailable(
                "another collector holds this instance's lease; only one may collect"
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._connection is not None:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
            )
            self._connection.close()
            self._connection = None


@dataclass
class ScheduledSource:
    adapter: SourceAdapter
    interval_seconds: int
    transform: Callable[[ObservationBatch], ObservationBatch] | None = None
    due_at: float = 0.0
    failures: int = 0
    last_result: dict = field(default_factory=dict)


def baseline_profile(settings: Settings) -> BaselineProfile | None:
    if not (settings.baseline_policy_version and settings.baseline_resource_uid):
        return None
    return BaselineProfile(
        version=settings.baseline_policy_version,
        resource_uid=settings.baseline_resource_uid,
        machine_type=settings.baseline_machine_type,
        region=settings.baseline_region,
        zone=settings.baseline_zone,
        project=settings.gcp_project,
        destinations=settings.baseline_destination_list,
        runners=settings.baseline_runner_list,
        assumptions={"unchanged_vm_size": True, "unchanged_vm_uptime": True},
        effective_from=settings.baseline_effective_from,
        effective_to=settings.baseline_effective_to,
    )


def resolve_baseline_profile(
    session: Session, tenant_id: uuid.UUID, settings: Settings
) -> BaselineProfile | None:
    """The configured profile, bounded by the period its policy version was saved with.

    Collection and reclassification both classify through this, so neither can
    apply a policy outside the period the other respects. A declared period is
    saved with its version the first time it is seen; afterwards the saved
    period governs even if configuration stops declaring one. A declared period
    that disagrees with the saved one classifies nothing, because preferring
    either would silently reprice history. With no period declared or saved,
    the policy covers all time, which the baseline self-check reports.
    """
    profile = baseline_profile(settings)
    if profile is None:
        return None
    saved = session.scalar(
        select(DeploymentPolicy).where(
            DeploymentPolicy.tenant_id == tenant_id, DeploymentPolicy.version == profile.version
        )
    )
    if saved_policy_conflict(saved, settings):
        return None
    if saved is not None:
        return replace(
            profile, effective_from=saved.effective_from, effective_to=saved.effective_to
        )
    if profile.effective_from is not None and session.get(Tenant, tenant_id) is not None:
        session.add(
            DeploymentPolicy(
                id=stable_id(str(tenant_id), "policy", profile.version),
                tenant_id=tenant_id,
                version=profile.version,
                effective_from=profile.effective_from,
                effective_to=profile.effective_to,
                baseline_resource_ids=[profile.resource_uid],
                assumptions=profile.assumptions,
                evidence="Declared by the RAINSTONE_BASELINE_* configuration.",
            )
        )
        session.flush()
    return profile


def baseline_transform(
    profile: BaselineProfile | None,
) -> Callable[[ObservationBatch], ObservationBatch]:
    def classify(batch: ObservationBatch) -> ObservationBatch:
        return replace(batch, jobs=tuple(classify_job(job, profile) for job in batch.jobs))

    return classify


def batch_targets(
    session: Session, tenant_id: uuid.UUID, settings: Settings
) -> list[BatchTarget]:
    """Batch resources to observe, resolved from Galaxy's recorded external IDs.

    The external ID is combined with the configured project and location; a name
    prefix is never used to infer environment or outcome.
    """
    rows = session.execute(
        select(Job.source_id, Job.state, ExecutionAttempt.external_id)
        .join(ExecutionAttempt, ExecutionAttempt.job_id == Job.id)
        .where(
            Job.tenant_id == tenant_id,
            ExecutionAttempt.external_id.is_not(None),
            ExecutionAttempt.runner.ilike("%batch%"),
        )
        .order_by(Job.source_id)
    ).all()
    targets: dict[tuple[str, str], BatchTarget] = {}
    for source_id, state, external_id in rows:
        name = external_id.rsplit("/", 1)[-1]
        targets[(source_id, name)] = BatchTarget(
            job_source_id=source_id,
            external_id=name,
            project=settings.gcp_project,
            location=settings.gcp_location,
            active=state not in TERMINAL_STATES,
        )
    return list(targets.values())


def tenant_id_for(settings: Settings, session: Session | None = None) -> uuid.UUID:
    """The active fact namespace for this instance.

    The slug names the namespace; its identifier is whatever enrollment
    generated, because replacing a source retires the old namespace under a
    suffixed slug and opens a new one. The derived identifier is only the
    fallback for a namespace that does not exist yet, such as fixture data.
    """
    if session is None:
        with Session(application_engine) as owned:
            return tenant_id_for(settings, owned)
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    return tenant.id if tenant is not None else stable_id("tenant", settings.tenant_slug)


class Collector:
    def __init__(
        self,
        settings: Settings,
        sources: Sequence[ScheduledSource],
        *,
        engine: Engine | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sources = list(sources)
        self._engine = engine or application_engine
        self._clock = clock
        self._sleep = sleeper
        self._tenant_id = tenant_id_for(settings)
        self._catalog_due = 0.0
        self._diagnostics_due = 0.0

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    def _backoff(self, failures: int) -> float:
        ceiling = min(self._settings.collect_max_backoff_seconds, 2 ** min(failures, 10))
        return ceiling / 2 + random.random() * ceiling / 2  # noqa: S311 - jitter only

    def collect_source(self, source: ScheduledSource) -> dict:
        with Session(self._engine) as session:
            cursor = read_cursor(session, self._tenant_id, source.adapter.source_name)
            try:
                batch = source.adapter.collect(cursor)
                if source.transform is not None:
                    batch = source.transform(batch)
            except Exception as error:  # noqa: BLE001 - surfaced as source status
                session.rollback()
                record_failure(session, self._tenant_id, source.adapter.source_name, repr(error))
                session.commit()
                source.failures += 1
                delay = self._backoff(source.failures)
                source.due_at = self._clock() + delay
                logger.warning(
                    "source %s failed (%s); retrying in %.1fs",
                    source.adapter.source_name,
                    type(error).__name__,
                    delay,
                )
                return {
                    "source": source.adapter.source_name,
                    "status": "failed",
                    "error": type(error).__name__,
                    "retry_in_seconds": round(delay, 1),
                }
            result = apply_batch(session, self._tenant_id, batch)
            revision = calculate_tenant(
                session, self._tenant_id, reason=f"{batch.source} collection"
            )
            # Facts, cursor and calculation commit together, so reports never
            # see observations without a matching revision, and an interrupted
            # cycle is simply re-collected.
            session.commit()
            source.failures = 0
            source.due_at = self._clock() + (0 if not batch.exhausted else source.interval_seconds)
            source.last_result = {**result, "revision": str(revision.id)}
            return source.last_result

    def heartbeat(self) -> None:
        """Record that the loop is alive, independently of dependency health."""
        path = self._settings.collector_heartbeat_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now(UTC).isoformat())
        except OSError as error:  # noqa: BLE001 - heartbeat is best effort
            logger.warning("could not write the collector heartbeat: %s", error)

    def record_capabilities(self) -> dict:
        """Run and persist the checks only this process has credentials for."""
        with Session(self._engine) as session:
            report = run_checks(session, self._settings, context="collector")
            record_report(session, self._tenant_id, report)
            session.commit()
        return {"source": "diagnostics", "status": report["overall_status"]}

    def refresh_catalog(self) -> dict:
        with Session(self._engine) as session:
            result = refresh_catalog(
                session,
                url=self._settings.catalog_feed_url,
                bundled_path=self._settings.catalog_path,
                require_signature=self._settings.catalog_require_signature,
                trusted_keys=parse_trusted_keys(self._settings.catalog_trusted_keys),
            )
            if result["status"] in {"refreshed", "bundled"}:
                # New prices change report facts, so recalculate rather than
                # leaving pinned snapshots stale until the next source batch.
                calculate_tenant(session, self._tenant_id, reason="price catalog refresh")
                session.commit()
            return result

    def run_once(self) -> list[dict]:
        now = self._clock()
        results = []
        self.heartbeat()
        if now >= self._diagnostics_due:
            results.append(self.record_capabilities())
            self._diagnostics_due = now + self._settings.diagnostics_interval_seconds
        if now >= self._catalog_due:
            results.append({"source": "price_catalog", **self.refresh_catalog()})
            self._catalog_due = now + self._settings.catalog_refresh_seconds
        for source in self._sources:
            if now >= source.due_at:
                results.append(self.collect_source(source))
        return results

    def next_delay(self) -> float:
        now = self._clock()
        due = [source.due_at for source in self._sources] + [
            self._catalog_due,
            self._diagnostics_due,
        ]
        return max(0.0, min(due) - now) if due else 1.0

    def run_forever(self, stop: Callable[[], bool] | None = None) -> None:
        should_stop = stop or (lambda: False)
        while not should_stop():
            for result in self.run_once():
                logger.info("collected %s", result)
            delay = min(self.next_delay(), 5.0)
            if delay > 0:
                self._sleep(delay)
            # Sleeping does not mean stalled, and a failing dependency does not
            # mean an unhealthy process.
            self.heartbeat()


def build_collector(settings: Settings | None = None) -> Collector:
    """Wire the sources this deployment is configured to observe."""
    settings = settings or get_settings()
    sources: list[ScheduledSource] = []

    if settings.galaxy_source_url is not None:
        galaxy_engine = source_engine(settings)
        capabilities = discover_capabilities(
            galaxy_engine, statement_timeout=settings.galaxy_statement_timeout
        )
        if not capabilities.compatible:
            raise RuntimeError(
                "the Galaxy source schema is not supported: missing "
                + ", ".join(capabilities.missing[:10])
            )
        with Session(application_engine) as session:
            verify_enrollment(
                session, settings, capabilities, read_evidence(settings, capabilities)
            )
            profile = resolve_baseline_profile(session, tenant_id_for(settings, session), settings)
            session.commit()

        sources.append(
            ScheduledSource(
                adapter=GalaxyDatabaseAdapter(
                    galaxy_engine,
                    batch_size=settings.galaxy_batch_size,
                    overlap_seconds=settings.galaxy_replay_overlap_seconds,
                    statement_timeout=settings.galaxy_statement_timeout,
                ),
                interval_seconds=settings.collect_interval_seconds,
                transform=baseline_transform(profile),
            )
        )

    if settings.kubernetes_enabled:
        sources.append(
            ScheduledSource(
                adapter=KubernetesCollector(
                    HttpKubernetesClient(
                        namespace=settings.kubernetes_namespace,
                        api_server=settings.kubernetes_api_server,
                    ),
                    # Both identities: the provider's numeric ID, and what
                    # Kubernetes calls the same machine. A node without a
                    # provider ID reports only the latter.
                    baseline_resource_ids=(
                        *(
                            (settings.baseline_resource_uid,)
                            if settings.baseline_resource_uid
                            else ()
                        ),
                        *settings.baseline_node_name_list,
                    ),
                    baseline_descriptor={
                        "machine_type": settings.baseline_machine_type,
                        "region": settings.baseline_region,
                        "zone": settings.baseline_zone,
                        "purchase_model": "on_demand",
                    },
                    watch_seconds=settings.kubernetes_watch_seconds,
                ),
                interval_seconds=1,
            )
        )

    if settings.gcp_batch_enabled:
        tenant_id = tenant_id_for(settings)

        def targets() -> list[BatchTarget]:
            with Session(application_engine) as session:
                return batch_targets(session, tenant_id, settings)

        sources.append(
            ScheduledSource(
                adapter=BatchCollector(
                    HttpGcpClient(),
                    targets,
                    max_targets=settings.gcp_max_targets_per_cycle,
                    enrich_compute=settings.gcp_enrich_compute,
                    enrich_logging=settings.gcp_enrich_logging,
                ),
                interval_seconds=settings.collect_batch_refresh_seconds,
            )
        )

    if not sources:
        raise RuntimeError(
            "no collection sources are configured; set the Galaxy source connection "
            "and enable the execution observers this deployment uses"
        )
    return Collector(settings, sources)


def reclassify(settings: Settings | None = None) -> dict:
    """Entry point for `rainstone reclassify-baseline`.

    Holds the collector's lease, so it never writes facts beside a running
    collector, and recalculates once so reports reflect the corrected facts.
    """
    settings = settings or get_settings()
    with CollectorLease(application_engine, settings.collector_lease_key), Session(
        application_engine
    ) as session:
        tenant_id = tenant_id_for(settings, session)
        conflict = saved_policy_conflict(
            session.scalar(select(DeploymentPolicy).where(
                DeploymentPolicy.tenant_id == tenant_id,
                DeploymentPolicy.version == settings.baseline_policy_version,
            )),
            settings,
        )
        if conflict:
            # Without a usable profile every baseline link would be removed.
            raise BaselinePolicyConflict(conflict)
        counts = reclassify_baseline(
            session, tenant_id, resolve_baseline_profile(session, tenant_id, settings)
        )
        revision = calculate_tenant(session, tenant_id, reason="baseline reclassification")
        session.commit()
        return {**counts, "revision_id": str(revision.id)}


def run(settings: Settings | None = None, *, cycles: int | None = None) -> list[dict]:
    """Entry point for `rainstone collect`."""
    settings = settings or get_settings()
    collector = build_collector(settings)
    stopping = {"value": False}

    def request_stop(*_: object) -> None:
        stopping["value"] = True
        logger.info("collector stopping after the current cycle")

    results: list[dict] = []
    with CollectorLease(application_engine, settings.collector_lease_key):
        if cycles is None:
            for received in (signal.SIGINT, signal.SIGTERM):
                signal.signal(received, request_stop)
            collector.run_forever(lambda: stopping["value"])
            return results
        for _ in range(cycles):
            results.extend(collector.run_once())
    return results
