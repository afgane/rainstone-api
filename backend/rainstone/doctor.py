"""Read-only self-checks for on-VM operation and acceptance.

`rainstone doctor` and the status endpoint report the same findings: database
connectivity and grants, source schema compatibility, owner and instance
mapping, Kubernetes list access, supported cloud reads, baseline resolution,
catalog availability and coverage, migration state, and collection freshness
with cursors and gaps.

Findings never include DSNs, passwords, tokens, raw job parameters or arbitrary
logs, so a sanitized report can be downloaded through the normal proxy. Every
probe is read-only; submitting synthetic jobs is a separate opt-in development
action.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from rainstone.adapters.galaxy_db import discover_capabilities
from rainstone.catalog import coverage as catalog_coverage
from rainstone.config import Settings, get_settings
from rainstone.costing import CALCULATION_VERSION
from rainstone.enrollment import configured_owners, source_engine
from rainstone.models import (
    CapabilityReport,
    CostRevision,
    IngestionState,
    InstallationRecord,
    ObservationGap,
    SourceBinding,
    Tenant,
)

STATUS_ORDER = {"fail": 3, "warn": 2, "skip": 1, "pass": 0}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    facts: dict = field(default_factory=dict)


def _application_database(session: Session) -> list[Check]:
    checks: list[Check] = []
    try:
        session.execute(text("SELECT 1"))
        checks.append(Check("application_database", "pass", "Rainstone database is reachable."))
    except Exception as error:  # noqa: BLE001 - reported as a finding
        return [
            Check(
                "application_database",
                "fail",
                f"Rainstone database is unreachable ({type(error).__name__}).",
            )
        ]
    revision = session.scalar(select(CostRevision).order_by(CostRevision.created_at.desc()))
    version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    checks.append(
        Check(
            "migration_state",
            "pass" if version else "fail",
            f"Schema revision {version or 'unknown'}.",
            {
                "alembic_revision": version,
                "calculation_version": CALCULATION_VERSION,
                "latest_revision_version": revision.calculation_version if revision else None,
                "latest_revision_at": revision.created_at.isoformat() if revision else None,
            },
        )
    )
    return checks


def _instance_identity(session: Session, settings: Settings) -> list[Check]:
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    if tenant is None:
        return [
            Check(
                "instance_identity",
                "fail",
                f"No enrolled instance '{settings.tenant_slug}'; run `rainstone enroll`.",
            )
        ]
    binding = session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant.id))
    if binding is not None:
        status, detail = "pass", "Instance identity is enrolled with a source database."
    elif settings.galaxy_source_url is not None:
        status, detail = "fail", "This instance has no source enrollment; run `rainstone enroll`."
    else:
        # Fixture and demo installations collect from no source, so there is
        # nothing to bind an identity to yet.
        status, detail = "warn", "No source database is configured, so no binding exists yet."
    checks = [
        Check(
            "instance_identity",
            status,
            detail,
            {
                "tenant": tenant.slug,
                "instance_uuid": str(binding.instance_uuid) if binding else None,
                "enrollment_uuid": str(binding.enrollment_uuid) if binding else None,
                "source_endpoint": binding.source_endpoint if binding else None,
                "source_version": tenant.source_version,
                "descriptor": (binding.descriptor if binding else {}),
            },
        )
    ]
    if settings.auth_mode == "anvil-workspace":
        account = settings.workspace_owner_source_id
        owners = configured_owners(session, tenant.id, account)
        checks.append(
            Check(
                "workspace_account",
                "pass" if len(owners) == 1 else "fail",
                "The configured shared Galaxy account resolves to one owner."
                if len(owners) == 1
                else f"The configured account '{account}' resolves to {len(owners)} owners.",
                {
                    "owner_source_id": owners[0].source_id if len(owners) == 1 else None,
                    "owner_label": owners[0].label if len(owners) == 1 else None,
                    "attribution": "shared Galaxy account, not individual humans",
                    "infrastructure_visible": settings.workspace_infrastructure_visible,
                },
            )
        )
    return checks


def _galaxy_source(settings: Settings) -> list[Check]:
    if settings.galaxy_source_url is None:
        return [Check("galaxy_source", "skip", "No Galaxy source database is configured.")]
    endpoint = settings.galaxy_source_endpoint or "unset"
    try:
        engine = source_engine(settings, pool_size=1)
        capabilities = discover_capabilities(
            engine, statement_timeout=settings.galaxy_statement_timeout
        )
    except Exception as error:  # noqa: BLE001 - reported as a finding
        return [
            Check(
                "galaxy_source",
                "fail",
                f"Galaxy source is unreachable at {endpoint} ({type(error).__name__}).",
            )
        ]
    privilege_detail = {
        "application-credential": (
            "Reads use Galaxy's own application credential. Read-only transactions, "
            "allowlisted statements, a statement timeout and a bounded pool are enforced by "
            "Rainstone, not by the database: this credential retains Galaxy's write privileges."
        ),
        "provisioned-reader": (
            "Reads use a provisioned role whose column-level grants the database enforces."
        ),
    }[settings.galaxy_source_privilege]
    fingerprint_detail = {
        "present": "the source's corroborating fingerprint is readable",
        "empty": "this Galaxy has no jobs yet, so no fingerprint can be read",
        "denied": "the source credential may not read the job table",
        "unavailable": "the fingerprint probe could not run",
    }.get(capabilities.fingerprint_status, capabilities.fingerprint_status)
    return [
        Check(
            "galaxy_source",
            "pass" if capabilities.compatible else "fail",
            "Galaxy schema is compatible and readable."
            if capabilities.compatible
            else "Galaxy schema or grants are insufficient.",
            {
                "endpoint": endpoint,
                "source_version": capabilities.source_version,
                "missing": list(capabilities.missing)[:20],
            },
        ),
        Check(
            "source_privilege",
            "pass" if settings.galaxy_source_privilege == "provisioned-reader" else "warn",
            privilege_detail,
            {"mode": settings.galaxy_source_privilege},
        ),
        Check(
            "source_evidence",
            "pass" if capabilities.fingerprint_status in {"present", "empty"} else "warn",
            f"Source evidence: {fingerprint_detail}. Enrollment identity lives in Rainstone's "
            "own database; this is corroboration, not proof of which database answered.",
            {"fingerprint_status": capabilities.fingerprint_status},
        ),
    ]


def _kubernetes(settings: Settings) -> list[Check]:
    if not settings.kubernetes_enabled:
        return [Check("kubernetes", "skip", "Kubernetes observation is disabled.")]
    from rainstone.adapters.kubernetes import HttpKubernetesClient

    client = HttpKubernetesClient(
        namespace=settings.kubernetes_namespace, api_server=settings.kubernetes_api_server
    )
    try:
        pods = client.list_pods()
        nodes = client.list_nodes()
    except Exception as error:  # noqa: BLE001 - reported as a capability gap
        return [
            Check(
                "kubernetes",
                "fail",
                f"Kubernetes list access is unavailable ({type(error).__name__}).",
                {"namespace": settings.kubernetes_namespace},
            )
        ]
    described = [
        item for item in nodes.get("items", [])
        if item.get("spec", {}).get("providerID")
    ]
    return [
        Check(
            "kubernetes",
            "pass",
            "Pods and nodes are listable in the configured namespace.",
            {
                "namespace": settings.kubernetes_namespace,
                "labelled_pods": len(pods.get("items", [])),
                "nodes": len(nodes.get("items", [])),
                "nodes_with_provider_id": len(described),
                "note": (
                    "Nodes without a provider ID give no verified VM identity; "
                    "placement stays qualified."
                ),
            },
        )
    ]


def _cloud(settings: Settings) -> list[Check]:
    if not settings.gcp_batch_enabled:
        return [Check("gcp_reads", "skip", "GCP Batch observation is disabled.")]
    from rainstone.adapters.gcp_batch import HttpGcpClient

    required = {
        "batch.jobs.list": True,
        "batch.jobs.get": True,
        "batch.tasks.list": True,
        "compute.instances.list": settings.gcp_enrich_compute,
        "compute.instances.get": settings.gcp_enrich_compute,
        "logging.logEntries.list": settings.gcp_enrich_logging,
    }
    try:
        results = HttpGcpClient().probe_access(settings.gcp_project, settings.gcp_location)
    except Exception as error:  # noqa: BLE001 - reported as a capability gap
        return [
            Check(
                "gcp_reads",
                "fail",
                f"Cloud reads could not be probed ({type(error).__name__}).",
                {"project": settings.gcp_project, "location": settings.gcp_location},
            )
        ]
    checks: list[Check] = []
    for operation, enabled in required.items():
        outcome = results.get(operation, "unavailable")
        if not enabled:
            status, detail = "skip", f"{operation} is not used by this configuration."
        elif outcome == "ok":
            status, detail = "pass", f"{operation} is permitted."
        elif outcome == "denied":
            status, detail = "fail", f"{operation} is denied for this deployment identity."
        else:
            status, detail = "fail", f"{operation} is {outcome}."
        checks.append(
            Check(
                f"gcp:{operation}",
                status,
                detail,
                {
                    "project": settings.gcp_project,
                    "location": settings.gcp_location,
                    "probe_result": outcome,
                },
            )
        )
    return checks


def _baseline(session: Session, settings: Settings) -> list[Check]:
    if not settings.baseline_policy_version:
        return [
            Check(
                "baseline_policy",
                "warn",
                "No baseline accounting policy is configured, so additional spend "
                "stays unavailable rather than zero.",
            )
        ]
    return [
        Check(
            "baseline_policy",
            "pass" if settings.baseline_resource_uid else "fail",
            "Baseline resource identity and assumptions are configured.",
            {
                "version": settings.baseline_policy_version,
                "resource_uid": settings.baseline_resource_uid,
                "machine_type": settings.baseline_machine_type,
                "region": settings.baseline_region,
                "destinations": list(settings.baseline_destination_list),
                "runners": list(settings.baseline_runner_list),
            },
        )
    ]


def _catalog(session: Session, settings: Settings) -> list[Check]:
    facts = catalog_coverage(session)
    if not facts["active_catalog_id"]:
        priced = bool(facts["supported"])
        return [
            Check(
                "price_catalog",
                "warn" if priced else "fail",
                "Prices are loaded but no catalog version is recorded; import a catalog "
                "artifact to record its provenance."
                if priced
                else "No price catalog is imported; dedicated work cannot be priced.",
                {
                    "feed_configured": bool(settings.catalog_feed_url),
                    "supported_combinations": len(facts["supported"]),
                },
            )
        ]
    observed = datetime.fromisoformat(facts["observed_at"])
    age_days = (datetime.now(UTC) - observed).days
    status = "pass" if settings.catalog_feed_url and age_days <= 7 else "warn"
    detail = (
        f"Active catalog {facts['active_catalog_id']} covers {len(facts['supported'])} "
        f"shape/region combinations; prices were observed {age_days} days ago."
    )
    if not settings.catalog_feed_url:
        detail += " No refresh feed is configured, so this is a pinned snapshot."
    return [
        Check(
            "price_catalog",
            status,
            detail,
            {
                "active_catalog_id": facts["active_catalog_id"],
                "observed_at": facts["observed_at"],
                "imported_at": facts["imported_at"],
                "signature_key_id": facts["signature_key_id"],
                "source_age_days": age_days,
                "supported_combinations": len(facts["supported"]),
                "regions": sorted({item["region"] for item in facts["supported"]}),
                "provenance": facts["provenance"],
            },
        )
    ]


def _collection(session: Session, settings: Settings) -> list[Check]:
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    if tenant is None:
        return []
    states = list(
        session.scalars(
            select(IngestionState)
            .where(IngestionState.tenant_id == tenant.id)
            .order_by(IngestionState.source)
        )
    )
    gaps = list(
        session.scalars(
            select(ObservationGap)
            .where(ObservationGap.tenant_id == tenant.id)
            .order_by(ObservationGap.detected_at.desc())
            .limit(10)
        )
    )
    now = datetime.now(UTC)
    checks: list[Check] = []
    for state in states:
        lag = (now - state.last_success_at).total_seconds() if state.last_success_at else None
        status = "pass"
        if state.status == "failed":
            status = "fail"
        elif state.status != "healthy" or (lag is not None and lag > 900):
            status = "warn"
        checks.append(
            Check(
                f"collection:{state.source}",
                status,
                f"Source {state.source} is {state.status}"
                + (f", last success {int(lag)}s ago." if lag is not None else ", never succeeded."),
                {
                    "cursor": state.cursor,
                    "metrics": state.metrics,
                    "consecutive_failures": state.consecutive_failures,
                    "error_type": (state.error or "").split("(")[0] or None,
                    "lag_seconds": int(lag) if lag is not None else None,
                },
            )
        )
    if not states:
        checks.append(
            Check("collection", "warn", "No source has reported a collection cycle yet.")
        )
    if gaps:
        checks.append(
            Check(
                "observation_gaps",
                "warn",
                f"{len(gaps)} recent observation gaps are recorded.",
                {
                    "gaps": [
                        {
                            "source": gap.source,
                            "kind": gap.kind,
                            "detected_at": gap.detected_at.isoformat(),
                            "recoverable": gap.recoverable,
                        }
                        for gap in gaps
                    ]
                },
            )
        )
    return checks


SOURCE_CONTEXTS = ("collector", "installation")
STALE_AFTER = timedelta(minutes=15)


def _overall(checks: list[dict]) -> str:
    worst = max((STATUS_ORDER[check["status"]] for check in checks), default=0)
    return next(name for name, value in STATUS_ORDER.items() if value == worst)


def run_checks(
    session: Session, settings: Settings | None = None, *, context: str = "web"
) -> dict:
    """Run the probes this process can actually perform.

    Only the collector and installation hold source and cloud credentials, so the
    web context runs local checks and reports their recorded findings instead of
    skipping probes it could never run.
    """
    settings = settings or get_settings()
    checks: list[Check] = []
    checks.extend(_application_database(session))
    checks.extend(_instance_identity(session, settings))
    if context != "web":
        checks.extend(_galaxy_source(settings))
        checks.extend(_kubernetes(settings))
        checks.extend(_cloud(settings))
    checks.extend(_baseline(session, settings))
    checks.extend(_catalog(session, settings))
    checks.extend(_collection(session, settings))
    serialized = [asdict(check) for check in checks]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "context": context,
        "overall_status": _overall(serialized),
        "auth_mode": settings.auth_mode,
        "tenant": settings.tenant_slug,
        "checks": serialized,
    }
    if context == "web":
        report = _merge_recorded(session, settings, report)
    report["failed_capabilities"] = [
        check["name"] for check in report["checks"] if check["status"] in {"fail", "warn"}
    ]
    return report


def _recorded(session: Session, tenant_id: uuid.UUID, context: str) -> CapabilityReport | None:
    return session.scalar(
        select(CapabilityReport).where(
            CapabilityReport.tenant_id == tenant_id, CapabilityReport.context == context
        )
    )


def _merge_recorded(session: Session, settings: Settings, report: dict) -> dict:
    """Add the collector's current findings and installation's recorded history.

    The collector is expected continuously, so its report ages into warnings.
    Bootstrap runs once per installation: its findings are history, they never
    decide current health, and a fresh collector check of the same capability
    supersedes them.
    """
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    now = datetime.now(UTC)
    current = list(report["checks"])
    history: list[dict] = []
    sources: list[dict] = []

    collector = _recorded(session, tenant.id, "collector") if tenant is not None else None
    collector_names: set[str] = set()
    if collector is None:
        current.append(
            asdict(
                Check(
                    "collector_diagnostics",
                    "warn",
                    "The collector has not recorded any capability checks yet, so source and "
                    "cloud access is unverified here.",
                )
            )
        )
    else:
        age = now - collector.generated_at
        stale = age > STALE_AFTER
        sources.append(
            {
                "context": "collector",
                "generated_at": collector.generated_at.isoformat(),
                "age_seconds": int(age.total_seconds()),
                "stale": stale,
                "overall_status": collector.overall_status,
                "kind": "current",
            }
        )
        for check in collector.report.get("checks", []):
            entry = dict(check)
            collector_names.add(entry["name"])
            entry["name"] = f"collector:{entry['name']}"
            entry["facts"] = {
                **entry.get("facts", {}),
                "recorded_at": collector.generated_at.isoformat(),
                "recorded_by": "collector",
                "stale": stale,
            }
            if stale:
                # A stale finding must never read as a current success.
                entry["status"] = "warn" if entry["status"] == "pass" else entry["status"]
                entry["detail"] = (
                    f"{entry['detail']} (recorded {int(age.total_seconds())}s ago; "
                    "the collector has not reported since)"
                )
            current.append(entry)

    installed = _recorded(session, tenant.id, "installation") if tenant is not None else None
    if installed is not None:
        age = now - installed.generated_at
        sources.append(
            {
                "context": "installation",
                "generated_at": installed.generated_at.isoformat(),
                "age_seconds": int(age.total_seconds()),
                "stale": False,
                "overall_status": installed.overall_status,
                "kind": "history",
            }
        )
        for check in installed.report.get("checks", []):
            if check["name"] in collector_names:
                # A current check of the same capability supersedes it.
                continue
            entry = dict(check)
            entry["name"] = f"installation:{entry['name']}"
            entry["history"] = True
            entry["detail"] = (
                f"{entry['detail']} (recorded during installation "
                f"{int(age.total_seconds())}s ago)"
            )
            entry["facts"] = {
                **entry.get("facts", {}),
                "recorded_at": installed.generated_at.isoformat(),
                "recorded_by": "installation",
                "history": True,
            }
            history.append(entry)

    report["checks"] = [*current, *history]
    report["recorded_reports"] = sources
    # Installation history does not decide current health.
    report["overall_status"] = _overall(current)
    return report


def schema_head() -> str | None:
    """The migration revision this release expects."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    except Exception:  # noqa: BLE001 - reported as an unknown head
        return None


def readiness(session: Session, settings: Settings | None = None) -> dict:
    """Can this process serve correct reports right now?

    Readiness is deliberately stricter than liveness: an unmigrated database or
    an unresolved fixed scope must keep a pod out of service rather than restart
    it.
    """
    settings = settings or get_settings()
    reasons: list[str] = []
    facts: dict = {"expected_schema": schema_head()}
    try:
        applied = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as error:  # noqa: BLE001 - reported as not ready
        session.rollback()
        return {
            "ready": False,
            "reasons": [f"the Rainstone database is unavailable ({type(error).__name__})"],
            "facts": facts,
        }
    facts["applied_schema"] = applied
    if applied is None:
        reasons.append("the Rainstone database has no applied migration")
    elif facts["expected_schema"] and applied != facts["expected_schema"]:
        reasons.append(
            f"the database is at migration {applied}, this release expects "
            f"{facts['expected_schema']}"
        )
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    facts["tenant"] = settings.tenant_slug
    if tenant is not None and settings.installation_id:
        facts["installation_id"] = settings.installation_id
        completed = session.scalar(
            select(InstallationRecord).where(
                InstallationRecord.tenant_id == tenant.id,
                InstallationRecord.installation_id == settings.installation_id,
            )
        )
        facts["installation_completed"] = completed is not None
        if completed is None:
            reasons.append(
                f"initialization for release {settings.installation_id} has not completed"
            )
    if tenant is None:
        reasons.append(f"instance '{settings.tenant_slug}' is not seeded yet")
    elif settings.auth_mode == "anvil-workspace":
        account = settings.workspace_owner_source_id
        owners = configured_owners(session, tenant.id, account)
        facts["workspace_account_matches"] = len(owners)
        if len(owners) != 1:
            reasons.append(
                f"the configured shared Galaxy account '{account}' resolves to {len(owners)} "
                "owners"
            )
    return {"ready": not reasons, "reasons": reasons, "facts": facts}


def mark_installed(session: Session, settings: Settings, details: dict | None = None) -> dict:
    """Record that this release's initialization sequence finished."""
    tenant = session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))
    if tenant is None:
        raise RuntimeError(
            f"instance '{settings.tenant_slug}' is not seeded, so initialization is incomplete"
        )
    if not settings.installation_id:
        return {"recorded": False, "reason": "no installation id is configured"}
    record = session.scalar(
        select(InstallationRecord).where(
            InstallationRecord.tenant_id == tenant.id,
            InstallationRecord.installation_id == settings.installation_id,
        )
    )
    if record is None:
        record = InstallationRecord(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            installation_id=settings.installation_id,
        )
        session.add(record)
    record.completed_at = datetime.now(UTC)
    record.details = details or {}
    session.flush()
    return {"recorded": True, "installation_id": settings.installation_id}


def record_report(session: Session, tenant_id: uuid.UUID, report: dict) -> None:
    """Persist a context's findings so the web process can show them."""
    context = report.get("context", "collector")
    row = session.scalar(
        select(CapabilityReport).where(
            CapabilityReport.tenant_id == tenant_id, CapabilityReport.context == context
        )
    )
    if row is None:
        row = CapabilityReport(id=uuid.uuid4(), tenant_id=tenant_id, context=context)
        session.add(row)
    row.generated_at = datetime.fromisoformat(report["generated_at"])
    row.overall_status = report["overall_status"]
    row.report = report
    session.flush()
