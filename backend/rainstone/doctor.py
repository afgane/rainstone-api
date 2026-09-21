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

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from rainstone.adapters.galaxy_db import discover_capabilities
from rainstone.catalog import coverage as catalog_coverage
from rainstone.config import Settings, get_settings
from rainstone.costing import CALCULATION_VERSION
from rainstone.models import (
    CostRevision,
    IngestionState,
    ObservationGap,
    Owner,
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


def _endpoint(url: str | None) -> str:
    """Host and port only: never a DSN with credentials."""
    if not url:
        return "unset"
    parts = urlsplit(url)
    return f"{parts.hostname or 'unknown'}:{parts.port}" if parts.port else (parts.hostname or "unknown")


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
                f"No seeded tenant '{settings.tenant_slug}'; run bootstrap.",
            )
        ]
    binding = session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant.id))
    if binding is not None:
        status, detail = "pass", "Instance identity is seeded and bound to a source database."
    elif settings.galaxy_database_url:
        status, detail = "fail", "Instance identity has no source binding; run bootstrap."
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
                "source_version": tenant.source_version,
                "descriptor": (binding.descriptor if binding else {}),
            },
        )
    ]
    if settings.auth_mode == "anvil-workspace":
        account = settings.workspace_owner_source_id
        owners = list(
            session.scalars(
                select(Owner).where(
                    Owner.tenant_id == tenant.id,
                    (Owner.source_id == account) | (Owner.label == account),
                )
            )
        )
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
    if not settings.galaxy_database_url:
        return [Check("galaxy_source", "skip", "No Galaxy source database is configured.")]
    endpoint = _endpoint(settings.galaxy_database_url)
    try:
        engine = create_engine(settings.galaxy_database_url, pool_pre_ping=True)
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
    return [
        Check(
            "galaxy_source",
            "pass" if capabilities.compatible else "fail",
            "Galaxy schema is compatible and readable with the scoped role."
            if capabilities.compatible
            else "Galaxy schema or grants are insufficient.",
            {
                "endpoint": endpoint,
                "source_version": capabilities.source_version,
                "missing": list(capabilities.missing)[:20],
            },
        )
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

    client = HttpGcpClient()
    try:
        probe = client.get_batch_job(settings.gcp_project, settings.gcp_location, "rainstone-probe")
        reachable = True
    except Exception as error:  # noqa: BLE001 - reported as a capability gap
        probe, reachable = None, False
        detail = f"Batch reads failed ({type(error).__name__})."
    if reachable:
        detail = (
            "Batch API answered; a missing probe job is expected."
            if probe is None
            else "Batch API answered."
        )
    return [
        Check(
            "gcp_reads",
            "pass" if reachable else "fail",
            detail,
            {
                "project": settings.gcp_project,
                "location": settings.gcp_location,
                "compute_enrichment": settings.gcp_enrich_compute,
                "logging_enrichment": settings.gcp_enrich_logging,
            },
        )
    ]


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


def run_checks(session: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    checks: list[Check] = []
    checks.extend(_application_database(session))
    checks.extend(_instance_identity(session, settings))
    checks.extend(_galaxy_source(settings))
    checks.extend(_kubernetes(settings))
    checks.extend(_cloud(settings))
    checks.extend(_baseline(session, settings))
    checks.extend(_catalog(session, settings))
    checks.extend(_collection(session, settings))
    worst = max((STATUS_ORDER[check.status] for check in checks), default=0)
    overall = next(name for name, value in STATUS_ORDER.items() if value == worst)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_status": overall,
        "auth_mode": settings.auth_mode,
        "tenant": settings.tenant_slug,
        "checks": [asdict(check) for check in checks],
        "failed_capabilities": [
            check.name for check in checks if check.status in {"fail", "warn"}
        ],
    }
