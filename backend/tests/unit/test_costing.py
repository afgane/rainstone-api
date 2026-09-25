from datetime import UTC, datetime, timedelta
from decimal import Decimal

from rainstone.costing import calculate_lifetime
from rainstone.models import (
    CapacityRelationship,
    PriceVersion,
    Quality,
    ResourceLifetime,
    ResourceSegment,
)

START = datetime(2026, 9, 19, tzinfo=UTC)


def lifetime(
    relationship: CapacityRelationship,
    seconds: str = "83.956263",
    timing_method: str = "compute_insert_complete_to_delete_request",
) -> ResourceLifetime:
    return ResourceLifetime(
        provider="gcp",
        resource_key="gce:demo/us-central1-a/1",
        resource_uid="1",
        region="us-central1",
        machine_type="n2-highmem-4",
        purchase_model="on_demand",
        capacity_relationship=relationship,
        observed_start=START,
        observed_end=START + timedelta(seconds=float(seconds)),
        timing_method=timing_method,
        facts={},
    )


def segment(start_offset: float, seconds: float, name: str = "lifetime") -> ResourceSegment:
    start = START + timedelta(seconds=start_offset)
    return ResourceSegment(
        source_segment_id=name,
        observed_start=start,
        observed_end=start + timedelta(seconds=seconds),
        timing_method="kubernetes_container_run",
        facts={},
    )


def price(rate: str = "0.262028") -> PriceVersion:
    return PriceVersion(hourly_rate=Decimal(rate), currency="USD")


def test_recorded_join_calculation_uses_lifecycle_proxy() -> None:
    line = calculate_lifetime(lifetime(CapacityRelationship.dedicated), [], price())[0]
    assert line.amount == Decimal("0.006110803244823333333333333334")
    assert line.quality == Quality.approximate


def test_minimum_billing_is_applied_once_to_the_lifetime() -> None:
    line = calculate_lifetime(lifetime(CapacityRelationship.dedicated, "10"), [], price("0.12"))[0]
    assert line.billed_seconds == Decimal("60")
    assert line.amount == Decimal("0.002")


def test_minimum_is_applied_once_across_several_segments() -> None:
    item = lifetime(CapacityRelationship.dedicated, "40")
    segments = [segment(0, 10, "first"), segment(20, 10, "second")]
    line = calculate_lifetime(item, segments, price("0.12"))[0]
    # Twenty observed seconds are uplifted to the one-minute minimum once.
    assert line.billed_seconds == Decimal("60")
    assert line.amount == Decimal("0.002")
    assert [allocation["charged_seconds"] for allocation in line.allocations] == ["30", "30"]


def test_price_boundary_splits_one_lifetime_and_applies_minimum_once() -> None:
    item = lifetime(CapacityRelationship.dedicated, "40")
    first = price("0.12")
    first.effective_from = None
    second = price("0.24")
    second.effective_from = START + timedelta(seconds=20)
    line = calculate_lifetime(item, [], [first, second])[0]
    assert line.billed_seconds == Decimal("60")
    assert line.amount == Decimal("0.0030")
    assert [allocation["charged_seconds"] for allocation in line.allocations] == ["30", "30"]


def test_existing_capacity_is_known_zero_but_allocation_is_unavailable() -> None:
    additional, allocated = calculate_lifetime(lifetime(CapacityRelationship.existing), [], price())
    assert additional.amount == 0
    assert additional.quality == Quality.known_zero
    assert allocated.amount is None
    assert allocated.quality == Quality.unpriced


def test_unknown_relationship_is_never_zero() -> None:
    lines = calculate_lifetime(lifetime(CapacityRelationship.unknown), [], None)
    assert all(line.amount is None for line in lines)


def test_unsupported_shape_stays_unpriced() -> None:
    item = lifetime(CapacityRelationship.dedicated)
    item.region = "us-east4"
    lines = calculate_lifetime(item, [], [])
    assert all(line.amount is None for line in lines)
    assert {line.quality for line in lines} == {Quality.unpriced}


def test_shared_vm_requires_an_allocation_policy() -> None:
    lines = calculate_lifetime(
        lifetime(CapacityRelationship.dedicated), [], price(), shared_job_count=2
    )
    assert all(line.amount is None for line in lines)
    assert all("allocation policy" in line.reason for line in lines)


def test_provider_billable_timing_is_complete_rather_than_approximate() -> None:
    line = calculate_lifetime(
        lifetime(CapacityRelationship.dedicated, timing_method="provider_billable"), [], price()
    )[0]
    assert line.quality == Quality.complete


def test_a_price_that_starts_after_the_work_is_never_applied_to_it() -> None:
    later = PriceVersion(
        hourly_rate=Decimal("0.13"), currency="USD", effective_from=START + timedelta(days=1)
    )
    lines = calculate_lifetime(lifetime(CapacityRelationship.dedicated), [], [later])
    assert all(line.amount is None for line in lines)
    assert {line.quality for line in lines} == {Quality.unpriced}
    assert all(later.effective_from.isoformat() in line.reason for line in lines)
