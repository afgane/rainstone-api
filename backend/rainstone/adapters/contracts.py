from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from rainstone.models import CapacityRelationship


@dataclass(frozen=True)
class NormalizedResourceInterval:
    source_interval_id: str
    resource_uid: str
    provider: str
    region: str | None
    machine_type: str | None
    purchase_model: str | None
    capacity_relationship: CapacityRelationship
    observed_start: datetime | None
    observed_end: datetime | None
    timing_method: str
    requested_vcpu: Decimal | None = None
    requested_memory_mib: Decimal | None = None
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedAttempt:
    job_source_id: str
    source_attempt_id: str
    runner: str
    external_id: str | None
    outcome: str
    tool_started_at: datetime | None
    tool_finished_at: datetime | None
    intervals: tuple[NormalizedResourceInterval, ...]


class ObservationAdapter(Protocol):
    source_name: str

    def observations(self) -> list[NormalizedAttempt]: ...
