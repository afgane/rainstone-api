"""Source enrollment, without writing to the Galaxy database.

Rainstone's source lifetime is a UUID generated here and kept in Rainstone's
own database. Reading Galaxy therefore needs no schema, no identity table and
no administrator credential; the default profile reads Galaxy with a credential
that already exists.

Endpoint, oldest-job fingerprint and deployment evidence corroborate that the
configured endpoint still holds the enrolled database. They are evidence, not
proof: a read-only client cannot see a restore behind an unchanged endpoint and
storage identity. Replacing the source is therefore an explicit act, and it
opens a new fact namespace rather than extending the old one.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from rainstone.adapters.galaxy_db import GalaxyCapabilities, discover_capabilities
from rainstone.config import Settings, get_settings
from rainstone.ingestion import stable_id
from rainstone.models import DeploymentPolicy, Owner, SourceBinding, Tenant

ACCOUNT_PROBE = text(
    "SELECT id::text AS source_id, username FROM galaxy_user "
    "WHERE username = :account OR id::text = :account OR email = :account"
)


class SourceReplacementDetected(RuntimeError):
    """The endpoint answered with evidence of a different database.

    Collection stops rather than merging two databases' job IDs, attempts,
    invocation memberships and cursors into one history.
    """


class SourceAccountUnresolved(RuntimeError):
    """The configured shared account is missing or ambiguous."""


class SourceNotEnrolled(RuntimeError):
    """No source lifetime has been enrolled for this instance yet."""


@dataclass(frozen=True)
class SourceEvidence:
    """Non-secret corroboration that this is still the enrolled source."""

    endpoint: str | None
    fingerprint: str | None
    fingerprint_status: str
    deployment: str = ""

    def as_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "fingerprint_status": self.fingerprint_status,
            "deployment": self.deployment,
        }


def source_engine(settings: Settings, *, pool_size: int = 2) -> Engine:
    """Open the source connection with a bounded pool.

    The default profile uses Galaxy's own application credential, so the
    read-only behavior around it — read-only transactions, allowlisted
    statements, a statement timeout and this bounded pool — is enforced by this
    process, not by the database.
    """
    url: URL | None = settings.galaxy_source_url
    if url is None:
        raise RuntimeError(
            "no Galaxy source connection is configured; set RAINSTONE_GALAXY_DB_HOST and "
            "RAINSTONE_GALAXY_DB_USER, or a complete RAINSTONE_GALAXY_DATABASE_URL"
        )
    return create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=0)


def read_evidence(
    settings: Settings, capabilities: GalaxyCapabilities
) -> SourceEvidence:
    return SourceEvidence(
        endpoint=settings.galaxy_source_endpoint,
        fingerprint=capabilities.source_fingerprint,
        fingerprint_status=capabilities.fingerprint_status,
        deployment=settings.source_deployment_evidence,
    )


def resolve_shared_account(
    engine: Engine, account: str, *, statement_timeout: str = "30s"
) -> tuple[str, str]:
    """Map the configured shared account to its Galaxy user ID and label.

    A username, a numeric ID or the exact configured email all resolve; the
    email is matched, never selected, so it is not copied into Rainstone.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{statement_timeout}'")
        matches = list(connection.execute(ACCOUNT_PROBE, {"account": account}).mappings())
    if not matches:
        raise SourceAccountUnresolved(f"the configured Galaxy account '{account}' does not exist")
    if len(matches) > 1:
        raise SourceAccountUnresolved(
            f"the configured Galaxy account '{account}' matches {len(matches)} users; "
            "name it by numeric ID so the scope is unambiguous"
        )
    return matches[0]["source_id"], matches[0]["username"] or account


def configured_owners(session: Session, tenant_id: uuid.UUID, account: str) -> list[Owner]:
    """The owners a configured shared-account string selects.

    One predicate for every caller. Readiness, diagnostics and request
    authorization must not disagree about which owner an installation's
    configured account names: a pod that serves reports while readiness says the
    scope is unresolved, or the reverse, is worse than either answer.
    """
    return list(
        session.scalars(
            select(Owner).where(
                Owner.tenant_id == tenant_id,
                (Owner.source_id == account)
                | (Owner.label == account)
                | (Owner.configured_as == account),
            )
        )
    )


def active_tenant(session: Session, settings: Settings) -> Tenant | None:
    """The fact namespace this instance currently collects into.

    The slug names the active namespace: a retired one keeps its history under
    a suffixed slug, so a plain slug lookup can never return it.
    """
    return session.scalar(select(Tenant).where(Tenant.slug == settings.tenant_slug))


def new_tenant_id(session: Session, settings: Settings) -> uuid.UUID:
    """A first enrollment derives its ID from the slug; a successor does not.

    Deriving every ID from the slug would collide with the namespace the
    replaced source retired under, which keeps that slug's derived identifier.
    """
    derived = stable_id("tenant", settings.tenant_slug)
    return derived if session.get(Tenant, derived) is None else uuid.uuid4()


def _archive(session: Session, tenant: Tenant) -> str:
    """Retire a fact namespace under a suffixed slug, keeping its history."""
    archived_at = datetime.now(UTC)
    base = tenant.slug[:70]
    for generation in range(1, 1000):
        candidate = f"{base}#{generation}"
        if session.scalar(select(Tenant).where(Tenant.slug == candidate)) is None:
            tenant.slug = candidate
            tenant.archived_at = archived_at
            session.flush()
            return candidate
    raise RuntimeError(f"too many retired fact namespaces for '{base}'")


def ensure_enrollment(
    session: Session,
    settings: Settings,
    capabilities: GalaxyCapabilities,
    evidence: SourceEvidence,
    *,
    reenroll: bool = False,
) -> tuple[Tenant, SourceBinding, dict]:
    """Resume this instance's enrollment, or open a new one when asked to.

    An endpoint change alone is not a replacement: a restart, a resize or a
    moved service keeps the same database. A changed oldest-job fingerprint is
    strong enough to stop on, and anything a read-only client cannot see at all
    is why replacement must be declared explicitly.
    """
    tenant = active_tenant(session, settings)
    binding = (
        session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant.id))
        if tenant is not None
        else None
    )
    notes: dict = {}

    if tenant is not None and reenroll:
        notes["retired_as"] = _archive(session, tenant)
        tenant, binding = None, None

    if binding is not None:
        notes.update(check_evidence(binding, evidence))
        notes["action"] = "resumed"
    else:
        now = datetime.now(UTC)
        if tenant is None:
            tenant = Tenant(
                id=new_tenant_id(session, settings),
                slug=settings.tenant_slug,
                display_name=settings.tenant_display_name,
                capabilities={},
            )
            session.add(tenant)
            session.flush()
        binding = SourceBinding(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            instance_uuid=uuid.uuid4(),
            enrollment_uuid=uuid.uuid4(),
            source_kind="galaxy_db",
            bound_at=now,
            enrolled_at=now,
        )
        session.add(binding)
        notes["action"] = "re-enrolled" if "retired_as" in notes else "enrolled"

    record_evidence(binding, capabilities, evidence)
    return tenant, binding, notes


def check_evidence(binding: SourceBinding, evidence: SourceEvidence) -> dict:
    """Compare corroborating evidence with what this instance enrolled.

    A changed oldest-job fingerprint is strong enough to stop on. A changed
    endpoint is not: a restart, resize or renamed service reaches the same
    database, so it is recorded and collection continues.
    """
    previous = binding.source_fingerprint
    if previous and evidence.fingerprint and previous != evidence.fingerprint:
        raise SourceReplacementDetected(
            "the configured endpoint answered with a different Galaxy database than this "
            "instance enrolled with. Collecting would merge two databases' job histories. "
            "Re-enroll explicitly (`rainstone enroll --replace-source`) to start a new fact "
            "namespace and keep the existing history separate"
        )
    if binding.source_endpoint and binding.source_endpoint != evidence.endpoint:
        return {"endpoint_changed_from": binding.source_endpoint}
    return {}


def record_evidence(
    binding: SourceBinding, capabilities: GalaxyCapabilities, evidence: SourceEvidence
) -> None:
    binding.source_endpoint = evidence.endpoint
    binding.source_fingerprint = evidence.fingerprint or binding.source_fingerprint
    binding.evidence = evidence.as_dict()
    # A schema change is a capability change, not a new source.
    binding.schema_fingerprint = capabilities.schema_fingerprint
    binding.source_version = capabilities.source_version or binding.source_version


def verify_enrollment(
    session: Session,
    settings: Settings,
    capabilities: GalaxyCapabilities,
    evidence: SourceEvidence,
) -> SourceBinding:
    """Confirm the collector is reading the source this instance enrolled with.

    Collection never enrolls: a deployment that has not run `rainstone enroll`
    has no resolved shared account either, and silently adopting whatever the
    endpoint answers is exactly what enrollment exists to prevent.
    """
    tenant = active_tenant(session, settings)
    binding = (
        session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant.id))
        if tenant is not None
        else None
    )
    if tenant is None or binding is None:
        raise SourceNotEnrolled(
            "this instance has no enrolled source; run "
            "`rainstone enroll --shared-account <account>` first"
        )
    check_evidence(binding, evidence)
    record_evidence(binding, capabilities, evidence)
    tenant.source_version = capabilities.source_version or tenant.source_version
    session.commit()
    return binding


def seed_instance(
    session: Session,
    settings: Settings,
    capabilities: GalaxyCapabilities,
    evidence: SourceEvidence,
    *,
    owner_source_id: str,
    owner_label: str,
    configured_as: str | None = None,
    descriptor: dict,
    reenroll: bool = False,
) -> dict:
    """Idempotently seed enrollment, tenant capabilities, owner and baseline."""
    tenant, binding, notes = ensure_enrollment(
        session, settings, capabilities, evidence, reenroll=reenroll
    )
    tenant.display_name = settings.tenant_display_name
    tenant.source_version = capabilities.source_version
    tenant.capabilities = {
        **(tenant.capabilities or {}),
        "galaxy_db": True,
        "kubernetes": settings.kubernetes_enabled,
        "gcp_batch": settings.gcp_batch_enabled,
        "demo": False,
        "source_privilege": settings.galaxy_source_privilege,
        "workspace_owner_source_id": owner_source_id,
    }
    tenant.synced_at = datetime.now(UTC)
    binding.descriptor = descriptor

    owner_id = stable_id(str(tenant.id), "owner", owner_source_id)
    owner = session.get(Owner, owner_id)
    if owner is None:
        owner = Owner(
            id=owner_id, tenant_id=tenant.id, source_id=owner_source_id, label=owner_label
        )
        session.add(owner)
    owner.label = owner_label
    owner.configured_as = configured_as if configured_as not in (owner_source_id, owner_label) else None

    policy_result = _seed_baseline(session, settings, tenant.id, descriptor)
    session.commit()
    return {
        "tenant": tenant.slug,
        "instance_uuid": str(binding.instance_uuid),
        "enrollment_uuid": str(binding.enrollment_uuid),
        "source_endpoint": binding.source_endpoint,
        "source_fingerprint_status": evidence.fingerprint_status,
        "owner_source_id": owner_source_id,
        "owner_label": owner_label,
        "baseline_policy": policy_result,
        "source_version": capabilities.source_version,
        **notes,
    }


def _seed_baseline(
    session: Session, settings: Settings, tenant_id: uuid.UUID, descriptor: dict
) -> str | None:
    if not (settings.baseline_policy_version and settings.baseline_resource_uid):
        return None
    policy_id = stable_id(str(tenant_id), "policy", settings.baseline_policy_version)
    policy = session.get(DeploymentPolicy, policy_id)
    if policy is None:
        policy = DeploymentPolicy(
            id=policy_id, tenant_id=tenant_id, version=settings.baseline_policy_version
        )
        session.add(policy)
        policy.effective_from = datetime.now(UTC)
    policy.baseline_resource_ids = [settings.baseline_resource_uid]
    policy.assumptions = {
        "unchanged_vm_size": True,
        "unchanged_vm_uptime": True,
        "machine_type": settings.baseline_machine_type,
        "region": settings.baseline_region,
        "zone": settings.baseline_zone,
        "destinations": list(settings.baseline_destination_list),
        "runners": list(settings.baseline_runner_list),
        "default_basis": "additional",
        "descriptor_source": descriptor.get("resolved_by", "configuration"),
    }
    policy.evidence = (
        "Discovered host descriptor; placement is verified per observation before any "
        "zero additional-spend statement."
    )
    return settings.baseline_policy_version


def enroll(
    settings: Settings | None = None,
    *,
    shared_account: str,
    descriptor: dict | None = None,
    replace_source: bool = False,
) -> dict:
    """Read the source with the configured credential and record enrollment."""
    settings = settings or get_settings()
    from rainstone.db import engine as application_engine

    engine = source_engine(settings)
    capabilities = discover_capabilities(
        engine, statement_timeout=settings.galaxy_statement_timeout
    )
    if not capabilities.compatible:
        raise RuntimeError(
            "the Galaxy source schema is not supported: missing "
            + ", ".join(capabilities.missing[:10])
        )
    owner_source_id, owner_label = resolve_shared_account(
        engine, shared_account, statement_timeout=settings.galaxy_statement_timeout
    )
    with Session(application_engine) as session:
        return seed_instance(
            session,
            settings,
            capabilities,
            read_evidence(settings, capabilities),
            owner_source_id=owner_source_id,
            owner_label=owner_label,
            configured_as=shared_account,
            descriptor=descriptor or {},
            reenroll=replace_source or settings.source_reenroll,
        )
