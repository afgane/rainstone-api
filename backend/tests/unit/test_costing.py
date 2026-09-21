from datetime import UTC, datetime, timedelta
from decimal import Decimal

from rainstone.costing import calculate_interval
from rainstone.models import CapacityRelationship, PriceVersion, Quality, ResourceInterval


def interval(relationship: CapacityRelationship, seconds: str = "83.956263") -> ResourceInterval:
    start = datetime(2026, 9, 19, tzinfo=UTC)
    return ResourceInterval(
        source_interval_id="x",
        resource_uid="vm",
        provider="gcp",
        region="us-central1",
        machine_type="n2-highmem-4",
        purchase_model="on_demand",
        capacity_relationship=relationship,
        observed_start=start,
        observed_end=start + timedelta(seconds=float(seconds)),
        timing_method="compute_insert_complete_to_delete_request",
        facts={},
    )


def price(rate: str = "0.262028") -> PriceVersion:
    return PriceVersion(hourly_rate=Decimal(rate), currency="USD")


def test_recorded_join_calculation_uses_lifecycle_proxy() -> None:
    line = calculate_interval(interval(CapacityRelationship.dedicated), price())[0]
    assert line.amount == Decimal("0.006110803244823333333333333334")
    assert line.quality == Quality.approximate


def test_minimum_billing_is_applied_once_to_vm_interval() -> None:
    line = calculate_interval(interval(CapacityRelationship.dedicated, "10"), price("0.12"))[0]
    assert line.billed_seconds == Decimal("60")
    assert line.amount == Decimal("0.002")


def test_price_boundary_splits_one_lifetime_and_applies_minimum_once() -> None:
    item = interval(CapacityRelationship.dedicated, "40")
    first = price("0.12")
    first.effective_from = None
    second = price("0.24")
    second.effective_from = item.observed_start + timedelta(seconds=20)
    line = calculate_interval(item, [first, second])[0]
    # 20 observed + 20 minimum-uplift seconds at the starting rate, then 20 at the new rate.
    assert line.billed_seconds == Decimal("60")
    assert line.amount == Decimal("0.002666666666666666666666666666")
    assert [allocation["charged_seconds"] for allocation in line.allocations] == ["40.0", "20.0"]


def test_existing_capacity_is_known_zero_but_allocation_is_unavailable() -> None:
    additional, allocated = calculate_interval(interval(CapacityRelationship.existing), price())
    assert additional.amount == 0
    assert additional.quality == Quality.known_zero
    assert allocated.amount is None
    assert allocated.quality == Quality.unpriced


def test_unknown_relationship_is_never_zero() -> None:
    lines = calculate_interval(interval(CapacityRelationship.unknown), None)
    assert all(line.amount is None for line in lines)
