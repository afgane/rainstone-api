import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rainstone.db import Base


class CapacityRelationship(str, enum.Enum):
    existing = "existing"
    dedicated = "dedicated"
    elastic_shared = "elastic_shared"
    unknown = "unknown"


class Quality(str, enum.Enum):
    complete = "complete"
    approximate = "approximate"
    partial = "partial"
    unpriced = "unpriced"
    in_progress = "in_progress"
    known_zero = "known_zero"


class Tenant(Base):
    __tablename__ = "tenant"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    source_version: Mapped[str | None] = mapped_column(String(100))
    base_url: Mapped[str | None] = mapped_column(String(500))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceBinding(Base):
    """Binds a tenant's seeded instance identity to one source database.

    Identity is the source-lifetime identifier enrolled in the source database
    itself, not a schema hash or a release name: column-level grants change what
    a reader can see, and two unrelated databases can share a schema. A replaced
    Galaxy database is refused until it is explicitly enrolled, so it cannot
    inherit an old instance identity and its job IDs.
    """

    __tablename__ = "source_binding"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), unique=True
    )
    instance_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    source_kind: Mapped[str] = mapped_column(String(40))
    source_identity: Mapped[str] = mapped_column(String(64))
    # Schema capability, recorded for diagnostics; never used as identity.
    schema_fingerprint: Mapped[str | None] = mapped_column(String(200))
    source_version: Mapped[str | None] = mapped_column(String(100))
    descriptor: Mapped[dict] = mapped_column(JSON, default=dict)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportGeneration(Base):
    """Per-tenant marker advanced whenever report-affecting facts change.

    Database triggers maintain it, so a direct write that bypasses the
    application still invalidates pinned report snapshots. Comparing this
    marker costs one small read per request, where hashing every fact row
    would cost a multiple of the job count.
    """

    __tablename__ = "report_generation"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("tenant_id", "source_id"),)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owner.id"))
    source_id: Mapped[str] = mapped_column(String(200))
    tool_id: Mapped[str] = mapped_column(String(500))
    tool_version: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(40))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    runner: Mapped[str | None] = mapped_column(String(100))
    destination: Mapped[str | None] = mapped_column(String(200))
    handler: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    copied_from_source_id: Mapped[str | None] = mapped_column(String(200))
    resource_hints: Mapped[dict] = mapped_column(JSON, default=dict)
    owner: Mapped[Owner] = relationship()
    attempts: Mapped[list["ExecutionAttempt"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id"),
        Index("ix_job_tenant_owner_created", "tenant_id", "owner_id", "created_at"),
        Index("ix_job_tool_version", "tenant_id", "tool_id", "tool_version"),
        Index("ix_job_tenant_updated", "tenant_id", "updated_at"),
    )


class JobStateEvent(Base):
    __tablename__ = "job_state_event"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("job_id", "state", "occurred_at"),)


class JobMetric(Base):
    """Allowlisted numeric Galaxy metrics, with their runner-specific units."""

    __tablename__ = "job_metric"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    plugin: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    numeric_value: Mapped[Decimal] = mapped_column(Numeric(30, 6))
    __table_args__ = (UniqueConstraint("job_id", "plugin", "name"),)


class Invocation(Base):
    __tablename__ = "invocation"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owner.id"))
    source_id: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str | None] = mapped_column(String(300))
    workflow_family_id: Mapped[str | None] = mapped_column(String(300))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invocation.id"))
    workflow_name: Mapped[str] = mapped_column(String(300))
    workflow_version: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(40))
    membership_settled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id"),
        Index("ix_invocation_workflow_identity", "tenant_id", "workflow_id", "workflow_version"),
    )


class InvocationJob(Base):
    __tablename__ = "invocation_job"
    invocation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invocation.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), primary_key=True)
    step_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    relationship: Mapped[str] = mapped_column(String(40), default="direct")


class DeploymentPolicy(Base):
    __tablename__ = "deployment_policy"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    version: Mapped[str] = mapped_column(String(100))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline_resource_ids: Mapped[list] = mapped_column(JSON)
    assumptions: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "version"),)


class PriceVersion(Base):
    __tablename__ = "price_version"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    catalog_id: Mapped[str] = mapped_column(String(200))
    machine_type: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(40))
    region: Mapped[str] = mapped_column(String(100))
    purchase_model: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3))
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("catalog_id", "machine_type", "region", "purchase_model"),)


class CatalogVersion(Base):
    """An immutable imported price catalog artifact and its provenance."""

    __tablename__ = "catalog_version"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    catalog_id: Mapped[str] = mapped_column(String(200), unique=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    artifact_digest: Mapped[str] = mapped_column(String(64))
    signature_key_id: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_count: Mapped[int] = mapped_column(Integer, default=0)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempt"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    source_attempt_id: Mapped[str] = mapped_column(String(300))
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("execution_attempt.id"))
    runner: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str | None] = mapped_column(String(500))
    outcome: Mapped[str] = mapped_column(String(40))
    provider_outcome: Mapped[str | None] = mapped_column(String(40))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    task_index: Mapped[int | None] = mapped_column(Integer)
    attempt_ordinal: Mapped[int | None] = mapped_column(Integer)
    tool_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tool_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("job_id", "source_attempt_id"),)


class ResourceLifetime(Base):
    """One chargeable provider resource lifetime.

    A lifetime is priced once per basis and component. Retries that reuse the
    same VM associate several attempts with this one record instead of storing
    and pricing the whole lifetime again under each attempt.
    """

    __tablename__ = "resource_lifetime"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40))
    resource_key: Mapped[str] = mapped_column(String(400))
    resource_uid: Mapped[str] = mapped_column(String(300))
    project: Mapped[str | None] = mapped_column(String(200))
    zone: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(100))
    machine_type: Mapped[str | None] = mapped_column(String(100))
    purchase_model: Mapped[str | None] = mapped_column(String(40))
    capacity_relationship: Mapped[CapacityRelationship] = mapped_column(
        Enum(CapacityRelationship, name="capacity_relationship")
    )
    observed_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing_method: Mapped[str | None] = mapped_column(String(200))
    requested_vcpu: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    requested_memory_mib: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "resource_key"),
        Index("ix_resource_lifetime_tenant", "tenant_id", "observed_start"),
    )


class ResourceSegment(Base):
    """A disjoint accounting segment of one resource lifetime."""

    __tablename__ = "resource_segment"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    lifetime_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_lifetime.id", ondelete="CASCADE")
    )
    source_segment_id: Mapped[str] = mapped_column(String(300))
    observed_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timing_method: Mapped[str | None] = mapped_column(String(200))
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("lifetime_id", "source_segment_id"),)


class LifetimeAttempt(Base):
    """Associates an execution attempt with the resource lifetime it used."""

    __tablename__ = "lifetime_attempt"
    lifetime_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_lifetime.id", ondelete="CASCADE"), primary_key=True
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_attempt.id", ondelete="CASCADE"), primary_key=True
    )
    task_index: Mapped[int | None] = mapped_column(Integer)
    attempt_ordinal: Mapped[int | None] = mapped_column(Integer)
    correlation: Mapped[str] = mapped_column(String(80), default="provider_event")
    facts: Mapped[dict] = mapped_column(JSON, default=dict)


class CostRevision(Base):
    __tablename__ = "cost_revision"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    calculation_version: Mapped[str] = mapped_column(String(100))
    input_digest: Mapped[str] = mapped_column(String(64))
    facts_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "calculation_version", "input_digest"),)


class CostLine(Base):
    __tablename__ = "cost_line"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cost_revision.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    lifetime_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_lifetime.id", ondelete="CASCADE")
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_attempt.id", ondelete="CASCADE")
    )
    basis: Mapped[str] = mapped_column(String(40))
    component: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    quality: Mapped[Quality] = mapped_column(Enum(Quality, name="cost_quality"))
    reason: Mapped[str] = mapped_column(Text)
    price_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("price_version.id"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deployment_policy.id"))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("revision_id", "lifetime_id", "job_id", "basis", "component"),
        Index("ix_cost_line_job_basis", "job_id", "basis"),
    )


class InfrastructureInterval(Base):
    __tablename__ = "infrastructure_interval"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(String(300))
    resource_uid: Mapped[str] = mapped_column(String(300))
    machine_type: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(100))
    observed_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    currency: Mapped[str] = mapped_column(String(3))
    quality: Mapped[Quality] = mapped_column(Enum(Quality, name="infra_quality"))
    __table_args__ = (UniqueConstraint("tenant_id", "source_id"),)


class IngestionEvent(Base):
    __tablename__ = "ingestion_event"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(300))
    payload_digest: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "source", "idempotency_key"),)


class IngestionState(Base):
    __tablename__ = "ingestion_state"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(80))
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "source"),)


class CapabilityReport(Base):
    """Self-check results recorded by the process that can actually run them.

    The web process holds no source or cloud credentials, so it reports the
    collector's and bootstrap's findings with their own timestamps instead of
    re-running probes it would always skip.
    """

    __tablename__ = "capability_report"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    context: Mapped[str] = mapped_column(String(40))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    overall_status: Mapped[str] = mapped_column(String(20))
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("tenant_id", "context"),)


class ObservationGap(Base):
    """A recorded loss of observation coverage that reports must not hide."""

    __tablename__ = "observation_gap"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(80))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gap_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recoverable: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text)
