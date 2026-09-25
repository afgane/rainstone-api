"""Deployment accounting: classify execution against the declared baseline.

The baseline profile says "this VM would already be running at this size for
this period". Work that provably ran on it adds no compute charge under that
assumption; work whose placement is not established stays unknown, never zero.

Jobs without execution evidence, such as paused or new jobs, never acquire a
fabricated resource interval or a minimum charge.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedJob,
    NormalizedLifetime,
    NormalizedSegment,
)
from rainstone.config import Settings
from rainstone.models import CapacityRelationship, DeploymentPolicy

TIMING_METHOD = "configured_baseline_occupancy"


@dataclass(frozen=True)
class BaselineProfile:
    """Boot-supplied identity and assumptions for the existing Galaxy VM."""

    version: str
    resource_uid: str
    machine_type: str | None = None
    region: str | None = None
    zone: str | None = None
    project: str | None = None
    provider: str = "gcp"
    purchase_model: str = "on_demand"
    destinations: tuple[str, ...] = ()
    runners: tuple[str, ...] = ("local",)
    evidence: str = ""
    assumptions: dict = field(default_factory=dict)
    # The half-open period the policy's assumptions hold for; open when unset.
    effective_from: datetime | None = None
    effective_to: datetime | None = None

    def covers(self, job: NormalizedJob) -> bool:
        return self.covers_placement(job.runner, job.destination)

    def covers_placement(self, runner: str | None, destination: str | None) -> bool:
        """Whether this configured placement is the baseline server.

        A named destination must be listed explicitly; a runner alone places
        only work that Galaxy recorded without a destination.
        """
        if destination and destination in self.destinations:
            return True
        return bool(runner and runner in self.runners and not destination)

    def applies_at(self, instant: datetime) -> bool:
        return (self.effective_from is None or instant >= self.effective_from) and (
            self.effective_to is None or instant < self.effective_to
        )


def _hint(job: NormalizedJob, key: str) -> Decimal | None:
    value = job.resource_hints.get(key)
    return Decimal(str(value)) if value is not None else None


def _occupancy(attempt: NormalizedAttempt) -> tuple[datetime | None, datetime | None]:
    return attempt.tool_started_at, attempt.tool_finished_at


def classify_job(job: NormalizedJob, profile: BaselineProfile | None) -> NormalizedJob:
    """Attach a baseline-capacity lifetime to attempts with execution evidence."""
    if profile is None or not profile.covers(job):
        return job
    attempts: list[NormalizedAttempt] = []
    for attempt in job.attempts:
        start, end = _occupancy(attempt)
        if start is None or attempt.lifetimes or not profile.applies_at(start):
            attempts.append(attempt)
            continue
        lifetime = NormalizedLifetime(
            provider=profile.provider,
            resource_key=(
                f"baseline:{profile.resource_uid}:job:{job.source_id}:"
                f"attempt:{attempt.source_attempt_id}"
            ),
            resource_uid=profile.resource_uid,
            capacity_relationship=CapacityRelationship.existing,
            timing_method=TIMING_METHOD,
            project=profile.project,
            zone=profile.zone,
            region=profile.region,
            machine_type=profile.machine_type,
            purchase_model=profile.purchase_model,
            observed_start=start,
            observed_end=end,
            requested_vcpu=_hint(job, "core.galaxy_slots"),
            requested_memory_mib=_hint(job, "core.galaxy_memory_mb"),
            segments=(
                NormalizedSegment(
                    source_segment_id="tool",
                    observed_start=start,
                    observed_end=end,
                    timing_method=TIMING_METHOD,
                ),
            ),
            facts={
                "policy_version": profile.version,
                "baseline_resource_uid": profile.resource_uid,
                "isolation": "shared_galaxy_host",
                "verified_vm_identity": False,
                "assumptions": profile.assumptions,
            },
        )
        attempts.append(replace(attempt, lifetimes=(lifetime,)))
    return replace(job, attempts=tuple(attempts))


def saved_policy_conflict(saved: DeploymentPolicy | None, settings: Settings) -> str | None:
    """Why a declared period cannot be applied, when it disagrees with the saved one."""
    if saved is None or settings.baseline_effective_from is None:
        return None
    declared = (settings.baseline_effective_from, settings.baseline_effective_to)
    if declared == (saved.effective_from, saved.effective_to):
        return None
    return (
        f"baseline policy {saved.version} was saved for "
        f"{saved.effective_from.isoformat()} to "
        f"{saved.effective_to.isoformat() if saved.effective_to else 'open'}, "
        "but a different period is configured; declare a new policy version to change it"
    )
