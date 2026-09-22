"""SKU selection and normalization: family/component matching, unit conversion,
ambiguity handling, and rejection of lookalike or malformed SKUs.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from gcp_pricing.mapping import MappingError, build_region_rates, classify_sku, price_point

NOW = datetime(2026, 9, 22, tzinfo=UTC)


def sku(
    sku_id: str,
    description: str,
    *,
    resource_family: str = "Compute",
    usage_type: str = "OnDemand",
    regions: list[str] | None = None,
    units: str = "0",
    nanos: int = 31_611_000,
    usage_unit: str = "h",
    currency: str = "USD",
    effective: str | None = "2026-01-01T00:00:00Z",
    start_usage: float = 0,
    tier_count: int = 1,
) -> dict:
    tier = {
        "startUsageAmount": start_usage,
        "unitPrice": {"currencyCode": currency, "units": units, "nanos": nanos},
    }
    return {
        "skuId": sku_id,
        "description": description,
        "category": {"resourceFamily": resource_family, "usageType": usage_type},
        "serviceRegions": regions if regions is not None else ["us-central1"],
        "pricingInfo": [
            {
                "effectiveTime": effective,
                "pricingExpression": {"usageUnit": usage_unit, "tieredRates": [tier] * tier_count},
                "currencyConversionRate": 1,
            }
        ],
    }


def test_n2_core_and_ram_are_classified() -> None:
    assert classify_sku(sku("A", "N2 Instance Core running in Americas")) == ("n2", "cpu")
    assert classify_sku(sku("B", "N2 Instance Ram running in Americas")) == ("n2", "ram")


def test_t2d_core_and_ram_are_classified() -> None:
    assert classify_sku(sku("A", "T2D AMD Instance Core running in Americas")) == ("t2d", "cpu")
    assert classify_sku(sku("B", "T2D AMD Instance Ram running in Americas")) == ("t2d", "ram")


@pytest.mark.parametrize(
    "description",
    [
        "N2D AMD Instance Core running in Americas",
        "N2 Custom Instance Core running in Americas",
        "T2A Instance Core running in Americas",
        "Spot Preemptible N2 Instance Core running in Americas",
        "Commitment v1: N2 Predefined Instance Core",
        "Sole Tenancy N2 Instance Core running in Americas",
        "Premium N2 Instance Core running in Americas",
    ],
)
def test_lookalikes_are_never_classified_as_n2_or_t2d(description: str) -> None:
    assert classify_sku(sku("X", description)) is None


def test_preemptible_usage_type_is_excluded_even_with_a_matching_description() -> None:
    candidate = sku("X", "N2 Instance Core running in Americas", usage_type="Preemptible")
    assert classify_sku(candidate) is None


def test_non_compute_resource_family_is_excluded() -> None:
    candidate = sku("X", "N2 Instance Core running in Americas", resource_family="Storage")
    assert classify_sku(candidate) is None


def test_cpu_unit_conversion_from_units_and_nanos() -> None:
    point = price_point(sku("A", "N2 Instance Core running in Americas", nanos=31_611_000), "cpu", now=NOW)
    assert point.rate_per_unit == Decimal("0.031611")


def test_ram_unit_conversion_from_units_and_nanos() -> None:
    point = price_point(
        sku("B", "N2 Instance Ram running in Americas", nanos=4_237_000, usage_unit="GiBy.h"),
        "ram",
        now=NOW,
    )
    assert point.rate_per_unit == Decimal("0.004237")


def test_a_mismatched_usage_unit_is_rejected() -> None:
    # A RAM SKU that (wrongly) carries the CPU usage unit must fail loudly
    # rather than be normalized as if it were per-vCPU.
    candidate = sku("B", "N2 Instance Ram running in Americas", usage_unit="h")
    with pytest.raises(MappingError, match="usage unit"):
        price_point(candidate, "ram", now=NOW)


def test_a_non_usd_price_is_rejected() -> None:
    with pytest.raises(MappingError, match="USD"):
        price_point(sku("A", "N2 Instance Core running in Americas", currency="EUR"), "cpu", now=NOW)


def test_a_tiered_price_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(MappingError, match="tier"):
        price_point(sku("A", "N2 Instance Core running in Americas", tier_count=2), "cpu", now=NOW)


def test_a_tier_that_does_not_start_at_zero_is_rejected() -> None:
    with pytest.raises(MappingError, match="zero usage"):
        price_point(sku("A", "N2 Instance Core running in Americas", start_usage=100), "cpu", now=NOW)


def test_a_future_dated_price_is_never_selected() -> None:
    candidate = sku("A", "N2 Instance Core running in Americas", effective="2099-01-01T00:00:00Z")
    with pytest.raises(MappingError, match="no pricing effective"):
        price_point(candidate, "cpu", now=NOW)


def test_the_latest_applicable_price_wins_over_an_older_one() -> None:
    candidate = sku("A", "N2 Instance Core running in Americas")
    candidate["pricingInfo"] = [
        {
            "effectiveTime": "2020-01-01T00:00:00Z",
            "pricingExpression": {
                "usageUnit": "h",
                "tieredRates": [
                    {"startUsageAmount": 0, "unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 1}}
                ],
            },
            "currencyConversionRate": 1,
        },
        {
            "effectiveTime": "2026-01-01T00:00:00Z",
            "pricingExpression": {
                "usageUnit": "h",
                "tieredRates": [
                    {
                        "startUsageAmount": 0,
                        "unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 31_611_000},
                    }
                ],
            },
            "currencyConversionRate": 1,
        },
    ]
    point = price_point(candidate, "cpu", now=NOW)
    assert point.rate_per_unit == Decimal("0.031611")
    assert point.effective_time == "2026-01-01T00:00:00Z"


def test_region_rates_pair_cpu_and_ram_by_family_and_region() -> None:
    skus = [
        sku("cpu-n2", "N2 Instance Core running in Americas", regions=["us-central1", "us-east4"]),
        sku(
            "ram-n2",
            "N2 Instance Ram running in Americas",
            regions=["us-central1", "us-east4"],
            nanos=4_237_000,
            usage_unit="GiBy.h",
        ),
    ]
    region_rates, problems = build_region_rates(skus, now=NOW)
    assert problems == []
    assert set(region_rates) == {("n2", "us-central1"), ("n2", "us-east4")}
    assert region_rates[("n2", "us-central1")].cpu.sku_id == "cpu-n2"
    assert region_rates[("n2", "us-central1")].ram.sku_id == "ram-n2"


def test_a_missing_ram_pair_blocks_that_family_region_but_not_others() -> None:
    skus = [
        sku("cpu-n2", "N2 Instance Core running in Americas", regions=["us-central1"]),
        # No matching RAM SKU for N2 at all.
        sku("cpu-t2d", "T2D AMD Instance Core running in Americas", regions=["us-central1"]),
        sku(
            "ram-t2d",
            "T2D AMD Instance Ram running in Americas",
            regions=["us-central1"],
            nanos=3_910_000,
            usage_unit="GiBy.h",
        ),
    ]
    region_rates, problems = build_region_rates(skus, now=NOW)
    assert ("n2", "us-central1") not in region_rates
    assert ("t2d", "us-central1") in region_rates
    assert any("n2 in us-central1 is missing its ram rate" in problem for problem in problems)


def test_duplicate_candidate_skus_for_the_same_family_region_are_ambiguous() -> None:
    skus = [
        sku("cpu-n2-a", "N2 Instance Core running in Americas", regions=["us-central1"]),
        sku("cpu-n2-b", "N2 Instance Core running in Americas", regions=["us-central1"]),
    ]
    region_rates, problems = build_region_rates(skus, now=NOW)
    assert region_rates == {}
    assert any("candidate cpu SKUs" in problem for problem in problems)


def test_no_cross_region_fallback_when_a_region_has_no_candidate_sku() -> None:
    skus = [
        sku("cpu-n2", "N2 Instance Core running in Americas", regions=["us-central1"]),
        sku(
            "ram-n2",
            "N2 Instance Ram running in Americas",
            regions=["us-central1"],
            nanos=4_237_000,
            usage_unit="GiBy.h",
        ),
    ]
    region_rates, _problems = build_region_rates(skus, now=NOW)
    assert "us-east4" not in {region for _family, region in region_rates}
