"""Assemble, sign and write the publisher's catalog artifact.

Reuses `rainstone.catalog.canonical_content` for the exact bytes that get
signed, so the publisher and the application never disagree about what a
signature covers.
"""

import base64
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rainstone.catalog import canonical_content

from gcp_pricing.mapping import ComponentRates
from gcp_pricing.mapping import shape_hourly_rate as _shape_hourly_rate
from gcp_pricing.shapes import MachineShape

REQUIRED_REGIONS = ("us-central1", "us-east4")
REQUIRED_FAMILIES = ("t2d", "n2")
SOURCE_URLS = (
    "https://docs.cloud.google.com/billing/v1/how-tos/catalog-api",
    "https://docs.cloud.google.com/compute/docs/general-purpose-machines",
)
COVERAGE_NOTE = (
    "T2D standard and N2 standard/highmem/highcpu on-demand compute prices "
    "only, for the regions listed above. Excludes Spot, custom machines, "
    "other families and providers, commitments, premium OS licenses, GPU, "
    "disk and network. Not a complete GCP price list."
)


class PublishError(ValueError):
    """The run has a problem that must block publication."""


def build_rates(
    shapes: tuple[MachineShape, ...],
    region_rates: dict[tuple[str, str], ComponentRates],
    *,
    observed_at: datetime,
    mapping_version: str,
) -> list[dict]:
    observed_iso = observed_at.isoformat().replace("+00:00", "Z")
    rates: list[dict] = []
    for (family, region), component_rates in sorted(region_rates.items()):
        for shape in shapes:
            if shape.family != family:
                continue
            rate = _shape_hourly_rate(shape, component_rates)
            rates.append(
                {
                    "provider": "gcp",
                    "region": region,
                    "purchase_model": "on_demand",
                    "machine_type": shape.machine_type,
                    "hourly_rate": str(rate),
                    "effective_from": observed_iso,
                    "provenance": {
                        "cpu_sku_id": component_rates.cpu.sku_id,
                        "cpu_sku_description": component_rates.cpu.description,
                        "cpu_rate_per_vcpu_hour": str(component_rates.cpu.rate_per_unit),
                        "cpu_usage_unit": component_rates.cpu.usage_unit,
                        "cpu_effective_time": component_rates.cpu.effective_time,
                        "ram_sku_id": component_rates.ram.sku_id,
                        "ram_sku_description": component_rates.ram.description,
                        "ram_rate_per_gib_hour": str(component_rates.ram.rate_per_unit),
                        "ram_usage_unit": component_rates.ram.usage_unit,
                        "ram_effective_time": component_rates.ram.effective_time,
                        "vcpu_count": shape.vcpu,
                        "memory_gib": str(shape.memory_gib),
                        "mapping_version": mapping_version,
                        "retrieved_at": observed_iso,
                    },
                }
            )
    return rates


def check_required_regions(
    region_rates: dict[tuple[str, str], ComponentRates],
    *,
    required_regions: tuple[str, ...] = REQUIRED_REGIONS,
    families: tuple[str, ...] = REQUIRED_FAMILIES,
) -> None:
    missing = [
        f"{family}/{region}"
        for family in families
        for region in required_regions
        if (family, region) not in region_rates
    ]
    if missing:
        raise PublishError("required regions are not priced: " + ", ".join(missing))


def _family_region_pairs(rates: list[dict]) -> set[tuple[str, str]]:
    return {(rate["machine_type"].split("-", 1)[0], rate["region"]) for rate in rates}


def check_no_regression(
    previous_rates: list[dict], new_rates: list[dict], *, acknowledged: tuple[tuple[str, str], ...] = ()
) -> None:
    """Refuse to silently drop a previously priced family/region.

    Coverage is compared by (family, region), not exact machine type: a
    family losing an entire region is the regression this guards against.
    Losing one shape while the family/region pair still prices other shapes
    is visible directly in the rates list, not flagged as a regression here.
    """
    lost = _family_region_pairs(previous_rates) - _family_region_pairs(new_rates) - set(acknowledged)
    if lost:
        raise PublishError(
            "coverage regression: previously priced but now missing: "
            + ", ".join(f"{family}/{region}" for family, region in sorted(lost))
        )


def catalog_id_for(observed_at: datetime) -> str:
    return f"gcp-t2d-n2-{observed_at.strftime('%Y%m%dT%H%M%SZ')}"


def build_catalog_document(
    rates: list[dict],
    *,
    observed_at: datetime,
    key_id: str,
    private_key: Ed25519PrivateKey,
    catalog_id: str | None = None,
) -> dict:
    if not rates:
        raise PublishError("refusing to publish a catalog with no priced rates")
    document = {
        "schema_version": 2,
        "catalog_id": catalog_id or catalog_id_for(observed_at),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "currency": "USD",
        "kind": "official_catalog_api",
        "historical_effective_time_available": False,
        "source_urls": list(SOURCE_URLS),
        "coverage": {
            "regions": sorted({rate["region"] for rate in rates}),
            "machine_families": list(REQUIRED_FAMILIES),
            "purchase_models": ["on_demand"],
            "complete": False,
            "note": COVERAGE_NOTE,
        },
        "rates": rates,
    }
    content = canonical_content(document)
    signature = private_key.sign(content)
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode(),
        "content_digest": hashlib.sha256(content).hexdigest(),
    }
    return document


def write_local(document: dict, output_dir: Path) -> tuple[Path, Path]:
    """Write the write-once version file and the refreshed `latest.json`.

    A rerun with the same `catalog_id` (the same publish minute) refuses to
    overwrite a version file that already exists, since published versions
    are immutable; `latest.json` is the only file this ever replaces.
    """
    versions_dir = output_dir / "gcp" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    version_path = versions_dir / f"{document['catalog_id']}.json"
    latest_path = output_dir / "gcp" / "latest.json"
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if version_path.exists():
        raise PublishError(f"{version_path} already exists; published catalog versions are write-once")
    version_path.write_text(payload)
    latest_path.write_text(payload)
    return version_path, latest_path


def load_previous_rates(source: str | None, *, timeout: int = 30) -> list[dict]:
    """Best-effort read of a previously published catalog's rates, for the
    regression check only. A missing prior artifact (first run, or a typo'd
    path the maintainer will notice from the warning) is not fatal by itself;
    an unreadable *existing* remote artifact is, since a silent empty read
    would defeat the entire regression check.
    """
    if not source:
        return []
    path = Path(source)
    if path.exists():
        return json.loads(path.read_text()).get("rates", [])
    if not source.startswith(("http://", "https://")):
        return []
    request = urllib.request.Request(source, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read()).get("rates", [])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise PublishError(f"could not read previous catalog at {source}: {error}") from error
    except urllib.error.URLError as error:
        raise PublishError(f"could not read previous catalog at {source}: {error}") from error
