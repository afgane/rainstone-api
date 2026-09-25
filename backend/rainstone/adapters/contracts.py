"""The versioned normalized ingestion contract shared by every adapter.

Source adapters produce these records; `rainstone.ingestion` is the only writer
of database rows. Keeping the contract explicit lets the same collector feed a
local database now and a hosted transport later without changing adapters.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from rainstone.models import CapacityRelationship

CONTRACT_VERSION = 2

# Galaxy's own record of a job's execution. Provider adapters observe the same
# execution separately, so this is evidence about an attempt, not an attempt of
# its own whenever provider evidence exists.
GALAXY_RECORD_ATTEMPT_ID = "galaxy-0"


@dataclass(frozen=True)
class NormalizedSegment:
    """One disjoint observed accounting segment of a resource lifetime."""

    source_segment_id: str
    observed_start: datetime | None
    observed_end: datetime | None
    timing_method: str
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedLifetime:
    """One chargeable provider resource, keyed independently of any attempt."""

    provider: str
    resource_key: str
    resource_uid: str
    capacity_relationship: CapacityRelationship
    timing_method: str
    project: str | None = None
    zone: str | None = None
    region: str | None = None
    machine_type: str | None = None
    purchase_model: str | None = None
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    requested_vcpu: Decimal | None = None
    requested_memory_mib: Decimal | None = None
    segments: tuple[NormalizedSegment, ...] = ()
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedAttempt:
    job_source_id: str
    source_attempt_id: str
    runner: str
    outcome: str
    external_id: str | None = None
    provider_outcome: str | None = None
    exit_code: int | None = None
    task_index: int | None = None
    attempt_ordinal: int | None = None
    tool_started_at: datetime | None = None
    tool_finished_at: datetime | None = None
    lifetimes: tuple[NormalizedLifetime, ...] = ()
    correlation: str = "provider_event"
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedMetric:
    plugin: str
    name: str
    numeric_value: Decimal


@dataclass(frozen=True)
class NormalizedStateEvent:
    state: str
    occurred_at: datetime


@dataclass(frozen=True)
class NormalizedOwner:
    source_id: str
    label: str
    is_admin: bool = False


@dataclass(frozen=True)
class NormalizedJob:
    source_id: str
    owner_source_id: str
    tool_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    tool_version: str | None = None
    exit_code: int | None = None
    runner: str | None = None
    destination: str | None = None
    handler: str | None = None
    copied_from_source_id: str | None = None
    resource_hints: dict = field(default_factory=dict)
    metrics: tuple[NormalizedMetric, ...] = ()
    state_events: tuple[NormalizedStateEvent, ...] = ()
    attempts: tuple[NormalizedAttempt, ...] = ()


@dataclass(frozen=True)
class NormalizedMembership:
    job_source_id: str
    step_key: str
    relationship: str


@dataclass(frozen=True)
class NormalizedInvocation:
    source_id: str
    owner_source_id: str
    workflow_name: str
    state: str
    created_at: datetime
    workflow_id: str | None = None
    workflow_family_id: str | None = None
    workflow_version: str | None = None
    parent_source_id: str | None = None
    membership_settled: bool = False
    memberships: tuple[NormalizedMembership, ...] = ()


@dataclass(frozen=True)
class NormalizedGap:
    source: str
    kind: str
    detail: str
    gap_start: datetime | None = None
    gap_end: datetime | None = None
    recoverable: bool = False


@dataclass(frozen=True)
class ObservationBatch:
    """One durable unit of work: commit it, then advance the cursor."""

    source: str
    observed_at: datetime
    contract_version: int = CONTRACT_VERSION
    owners: tuple[NormalizedOwner, ...] = ()
    jobs: tuple[NormalizedJob, ...] = ()
    invocations: tuple[NormalizedInvocation, ...] = ()
    attempts: tuple[NormalizedAttempt, ...] = ()
    gaps: tuple[NormalizedGap, ...] = ()
    cursor: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    exhausted: bool = True


class SourceAdapter(Protocol):
    """A bounded, resumable source of normalized observations."""

    source_name: str

    def collect(self, cursor: dict) -> ObservationBatch: ...
