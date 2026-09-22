"""Select T2D/N2 on-demand CPU and RAM SKUs and normalize them to hourly rates.

https://docs.cloud.google.com/billing/docs/reference/rest/v1/services.skus/list

Matching combines structured category fields (which family alone cannot
distinguish, since categories group several similar families together) with
an *anchored* description prefix, so a lookalike such as "N2 Custom Instance
Core running in Americas" or "N2D AMD Instance Core running in Americas" does
not accidentally match the "N2 Instance Core" prefix. A short excluded-terms
list is a second, independent guard against the same lookalikes.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from gcp_pricing.shapes import MachineShape

MAPPING_VERSION = "gcp-t2d-n2-v1"

Component = Literal["cpu", "ram"]

# Each family's on-demand predefined CPU and RAM SKU descriptions are shared
# by every variant (standard/highmem/highcpu): GCP charges per provisioned
# vCPU and per provisioned GiB regardless of which named variant asked for
# them, so one CPU/RAM rate per family/region prices every variant's shapes.
CPU_PATTERNS: dict[str, re.Pattern[str]] = {
    "t2d": re.compile(r"^T2D AMD Instance Core running in"),
    "n2": re.compile(r"^N2 Instance Core running in"),
}
RAM_PATTERNS: dict[str, re.Pattern[str]] = {
    "t2d": re.compile(r"^T2D AMD Instance Ram running in"),
    "n2": re.compile(r"^N2 Instance Ram running in"),
}
EXCLUDED_DESCRIPTION_TERMS = (
    "Custom",
    "Sole Tenancy",
    "Premium",
    "Spot",
    "Preemptible",
    "Commitment",
    "Reserved",
    "Extreme",
)
EXPECTED_USAGE_UNIT: dict[Component, str] = {"cpu": "h", "ram": "GiBy.h"}


class MappingError(ValueError):
    """A SKU could not be normalized into a trustworthy hourly component rate."""


@dataclass(frozen=True)
class PricePoint:
    sku_id: str
    description: str
    rate_per_unit: Decimal
    usage_unit: str
    effective_time: str | None


@dataclass(frozen=True)
class ComponentRates:
    """One family's CPU and RAM price points, applicable in one region."""

    cpu: PricePoint
    ram: PricePoint


def classify_sku(sku: dict) -> tuple[str, Component] | None:
    category = sku.get("category") or {}
    if category.get("resourceFamily") != "Compute":
        return None
    if category.get("usageType") != "OnDemand":
        return None
    description = sku.get("description") or ""
    if any(term in description for term in EXCLUDED_DESCRIPTION_TERMS):
        return None
    for family, pattern in CPU_PATTERNS.items():
        if pattern.match(description):
            return family, "cpu"
    for family, pattern in RAM_PATTERNS.items():
        if pattern.match(description):
            return family, "ram"
    return None


def _money_to_decimal(unit_price: dict, *, sku_id: str) -> Decimal:
    try:
        units = Decimal(str(unit_price.get("units", "0")))
        nanos = Decimal(int(unit_price.get("nanos", 0))) / Decimal("1000000000")
    except InvalidOperation as error:
        raise MappingError(f"SKU {sku_id} unit price is not decimal: {unit_price}") from error
    return units + nanos


def _select_pricing_info(sku: dict, *, now: datetime) -> dict:
    """The latest pricing entry already effective at the retrieval time.

    Never the newest entry outright: a future-dated price change must not be
    published before it actually takes effect.
    """
    sku_id = sku.get("skuId", "?")
    infos = sku.get("pricingInfo") or []
    if not infos:
        raise MappingError(f"SKU {sku_id} has no pricingInfo")
    applicable: list[tuple[datetime, dict]] = []
    for info in infos:
        effective = info.get("effectiveTime")
        when = datetime.min.replace(tzinfo=UTC)
        if effective:
            when = datetime.fromisoformat(effective.replace("Z", "+00:00"))
            if when > now:
                continue
        applicable.append((when, info))
    if not applicable:
        raise MappingError(f"SKU {sku_id} has no pricing effective by {now.isoformat()}")
    applicable.sort(key=lambda pair: pair[0])
    return applicable[-1][1]


def price_point(sku: dict, component: Component, *, now: datetime) -> PricePoint:
    sku_id = sku.get("skuId", "?")
    info = _select_pricing_info(sku, now=now)
    if info.get("currencyConversionRate", 1) != 1:
        raise MappingError(f"SKU {sku_id} carries a non-unit currency conversion rate")
    expression = info.get("pricingExpression") or {}
    usage_unit = expression.get("usageUnit")
    if usage_unit != EXPECTED_USAGE_UNIT[component]:
        raise MappingError(
            f"SKU {sku_id} ({component}) has usage unit {usage_unit!r}, expected "
            f"{EXPECTED_USAGE_UNIT[component]!r}"
        )
    tiers = expression.get("tieredRates") or []
    if len(tiers) != 1:
        raise MappingError(f"SKU {sku_id} has {len(tiers)} pricing tiers, expected exactly one")
    tier = tiers[0]
    if Decimal(str(tier.get("startUsageAmount", 0))) != 0:
        raise MappingError(f"SKU {sku_id} tier does not start at zero usage")
    unit_price = tier.get("unitPrice") or {}
    if unit_price.get("currencyCode") != "USD":
        raise MappingError(f"SKU {sku_id} is not priced in USD")
    return PricePoint(
        sku_id=sku_id,
        description=sku.get("description", ""),
        rate_per_unit=_money_to_decimal(unit_price, sku_id=sku_id),
        usage_unit=usage_unit,
        effective_time=info.get("effectiveTime"),
    )


def build_region_rates(
    skus: list[dict], *, now: datetime
) -> tuple[dict[tuple[str, str], ComponentRates], list[str]]:
    """CPU+RAM rate pairs keyed by (family, region), plus a list of problems.

    A problem names a family/region combination this run could not price: an
    ambiguous SKU set, a malformed price, or a component missing its pair.
    Nothing here ever substitutes another region's rate for a missing one.
    """
    candidates: dict[tuple[str, Component, str], list[dict]] = defaultdict(list)
    for sku in skus:
        classified = classify_sku(sku)
        if classified is None:
            continue
        family, component = classified
        for region in sku.get("serviceRegions") or []:
            candidates[(family, component, region)].append(sku)

    problems: list[str] = []
    points: dict[tuple[str, Component, str], PricePoint] = {}
    for (family, component, region), matches in candidates.items():
        if len(matches) != 1:
            ids = ", ".join(sku.get("skuId", "?") for sku in matches)
            problems.append(
                f"{len(matches)} candidate {component} SKUs for {family} in {region}: {ids}"
            )
            continue
        try:
            points[(family, component, region)] = price_point(matches[0], component, now=now)
        except MappingError as error:
            problems.append(str(error))

    by_family_region: dict[tuple[str, str], dict[Component, PricePoint]] = defaultdict(dict)
    for (family, component, region), point in points.items():
        by_family_region[(family, region)][component] = point

    region_rates: dict[tuple[str, str], ComponentRates] = {}
    for (family, region), parts in by_family_region.items():
        if "cpu" in parts and "ram" in parts:
            region_rates[(family, region)] = ComponentRates(cpu=parts["cpu"], ram=parts["ram"])
        else:
            missing = "ram" if "cpu" in parts else "cpu"
            problems.append(f"{family} in {region} is missing its {missing} rate")
    return region_rates, problems


def shape_hourly_rate(shape: MachineShape, rates: ComponentRates) -> Decimal:
    return Decimal(shape.vcpu) * rates.cpu.rate_per_unit + shape.memory_gib * rates.ram.rate_per_unit
