import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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
    runner: Mapped[str | None] = mapped_column(String(100))
    destination: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    copied_from_source_id: Mapped[str | None] = mapped_column(String(200))
    owner: Mapped[Owner] = relationship()
    attempts: Mapped[list["ExecutionAttempt"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id"),
        Index("ix_job_tenant_owner_created", "tenant_id", "owner_id", "created_at"),
        Index("ix_job_tool_version", "tenant_id", "tool_id", "tool_version"),
    )


class Invocation(Base):
    __tablename__ = "invocation"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("owner.id"))
    source_id: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str | None] = mapped_column(String(300))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invocation.id"))
    workflow_name: Mapped[str] = mapped_column(String(300))
    workflow_version: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(40))
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


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempt"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    source_attempt_id: Mapped[str] = mapped_column(String(300))
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("execution_attempt.id"))
    runner: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str | None] = mapped_column(String(500))
    outcome: Mapped[str] = mapped_column(String(40))
    tool_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tool_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("job_id", "source_attempt_id"),)


class ResourceInterval(Base):
    __tablename__ = "resource_interval"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("execution_attempt.id", ondelete="CASCADE"))
    source_interval_id: Mapped[str] = mapped_column(String(300))
    resource_uid: Mapped[str] = mapped_column(String(300))
    provider: Mapped[str] = mapped_column(String(40))
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
    __table_args__ = (UniqueConstraint("attempt_id", "source_interval_id"),)


class CostRevision(Base):
    __tablename__ = "cost_revision"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    calculation_version: Mapped[str] = mapped_column(String(100))
    input_digest: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "calculation_version", "input_digest"),)


class CostLine(Base):
    __tablename__ = "cost_line"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cost_revision.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("execution_attempt.id", ondelete="CASCADE"))
    resource_interval_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_interval.id", ondelete="CASCADE")
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
        UniqueConstraint("revision_id", "resource_interval_id", "basis", "component"),
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
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "source"),)
