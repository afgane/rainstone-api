"""Catalog document assembly, signing, regression checks and local writing."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from gcp_pricing.mapping import ComponentRates, PricePoint
from gcp_pricing.output import (
    PublishError,
    build_catalog_document,
    build_rates,
    catalog_id_for,
    check_no_regression,
    check_required_regions,
    load_previous_rates,
    write_local,
)
from gcp_pricing.shapes import MachineShape
from rainstone.catalog import CatalogError, canonical_content, validate

NOW = datetime(2026, 10, 1, 6, 17, 0, tzinfo=UTC)


def cpu(rate: str, sku_id: str = "cpu-sku") -> PricePoint:
    return PricePoint(sku_id, "N2 Instance Core running in Americas", Decimal(rate), "h", "2026-01-01T00:00:00Z")


def ram(rate: str, sku_id: str = "ram-sku") -> PricePoint:
    return PricePoint(sku_id, "N2 Instance Ram running in Americas", Decimal(rate), "GiBy.h", "2026-01-01T00:00:00Z")


def shape(machine_type: str = "n2-standard-2", family: str = "n2", vcpu: int = 2, memory_gib: str = "8") -> MachineShape:
    return MachineShape(machine_type, family, "standard", vcpu, Decimal(memory_gib), "https://example.invalid")


def signing_key() -> tuple[Ed25519PrivateKey, bytes]:
    key = Ed25519PrivateKey.generate()
    return key, key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def test_build_rates_computes_the_documented_formula() -> None:
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    assert len(rates) == 1
    # 2 vCPU * 0.031611 + 8 GiB * 0.004237 == 0.097118, exactly.
    assert rates[0]["hourly_rate"] == "0.097118"
    assert rates[0]["region"] == "us-central1"
    assert rates[0]["purchase_model"] == "on_demand"
    assert rates[0]["effective_from"] == "2026-10-01T06:17:00Z"


def test_rate_provenance_carries_everything_the_spec_requires() -> None:
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="gcp-t2d-n2-v1",
    )
    provenance = rates[0]["provenance"]
    for field in (
        "cpu_sku_id",
        "cpu_sku_description",
        "cpu_rate_per_vcpu_hour",
        "cpu_usage_unit",
        "cpu_effective_time",
        "ram_sku_id",
        "ram_sku_description",
        "ram_rate_per_gib_hour",
        "ram_usage_unit",
        "ram_effective_time",
        "vcpu_count",
        "memory_gib",
        "mapping_version",
        "retrieved_at",
    ):
        assert field in provenance, field
    assert provenance["vcpu_count"] == 2
    assert provenance["memory_gib"] == "8"
    assert provenance["mapping_version"] == "gcp-t2d-n2-v1"


def test_a_rerun_gets_a_different_catalog_id() -> None:
    later = datetime(2026, 11, 1, 6, 17, 0, tzinfo=UTC)
    assert catalog_id_for(NOW) != catalog_id_for(later)
    assert catalog_id_for(NOW) == "gcp-t2d-n2-20261001T061700Z"


def test_document_declares_a_snapshot_not_historical_reconstruction() -> None:
    key, _public = signing_key()
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    document = build_catalog_document(rates, observed_at=NOW, key_id="k", private_key=key)
    assert document["historical_effective_time_available"] is False
    assert document["kind"] == "official_catalog_api"
    assert document["schema_version"] == 2
    assert document["coverage"]["machine_families"] == ["t2d", "n2"]
    assert document["coverage"]["complete"] is False


def test_refusing_to_publish_an_empty_catalog() -> None:
    key, _public = signing_key()
    with pytest.raises(PublishError, match="no priced rates"):
        build_catalog_document([], observed_at=NOW, key_id="k", private_key=key)


def test_the_signed_document_verifies_and_imports_through_rainstones_own_validator() -> None:
    key, public = signing_key()
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    document = build_catalog_document(rates, observed_at=NOW, key_id="release-2026", private_key=key)
    validated = validate(
        json.dumps(document).encode(),
        source="local",
        require_signature=True,
        trusted_keys={"release-2026": public},
    )
    assert validated.signature_verified is True
    assert validated.catalog_id == document["catalog_id"]
    assert len(validated.rates) == 1


def test_tampering_with_a_rate_breaks_the_signature() -> None:
    """Even an attacker who recomputes the self-declared digest to match their
    tampered content cannot forge the Ed25519 signature over that content.
    """
    key, public = signing_key()
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    document = build_catalog_document(rates, observed_at=NOW, key_id="release-2026", private_key=key)
    document["rates"][0]["hourly_rate"] = "0.000001"
    document["signature"]["content_digest"] = hashlib.sha256(canonical_content(document)).hexdigest()
    with pytest.raises(CatalogError, match="does not verify"):
        validate(
            json.dumps(document).encode(),
            source="local",
            require_signature=True,
            trusted_keys={"release-2026": public},
        )


def test_required_regions_must_be_priced_for_both_families() -> None:
    with pytest.raises(PublishError, match="t2d/us-east4"):
        check_required_regions({("t2d", "us-central1"): None, ("n2", "us-central1"): None, ("n2", "us-east4"): None})


def test_required_regions_pass_when_both_families_cover_both_regions() -> None:
    check_required_regions(
        {
            ("t2d", "us-central1"): None,
            ("t2d", "us-east4"): None,
            ("n2", "us-central1"): None,
            ("n2", "us-east4"): None,
        }
    )


def test_losing_a_previously_priced_region_blocks_publication() -> None:
    previous = [{"machine_type": "n2-standard-2", "region": "us-east4"}]
    new = [{"machine_type": "n2-standard-2", "region": "us-central1"}]
    with pytest.raises(PublishError, match="n2/us-east4"):
        check_no_regression(previous, new)


def test_an_acknowledged_regression_is_allowed_through() -> None:
    previous = [{"machine_type": "n2-standard-2", "region": "us-east4"}]
    new = [{"machine_type": "n2-standard-2", "region": "us-central1"}]
    check_no_regression(previous, new, acknowledged=(("n2", "us-east4"),))


def test_new_unambiguous_regions_are_not_a_regression() -> None:
    previous = [{"machine_type": "n2-standard-2", "region": "us-central1"}]
    new = [
        {"machine_type": "n2-standard-2", "region": "us-central1"},
        {"machine_type": "n2-standard-2", "region": "us-east4"},
    ]
    check_no_regression(previous, new)


def test_write_local_writes_the_version_and_latest_files(tmp_path: Path) -> None:
    key, _public = signing_key()
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    document = build_catalog_document(rates, observed_at=NOW, key_id="k", private_key=key)
    version_path, latest_path = write_local(document, tmp_path)
    assert json.loads(version_path.read_text())["catalog_id"] == document["catalog_id"]
    assert json.loads(latest_path.read_text()) == json.loads(version_path.read_text())


def test_write_local_refuses_to_overwrite_a_published_version(tmp_path: Path) -> None:
    key, _public = signing_key()
    rates = build_rates(
        (shape(),),
        {("n2", "us-central1"): ComponentRates(cpu("0.031611"), ram("0.004237"))},
        observed_at=NOW,
        mapping_version="v1",
    )
    document = build_catalog_document(rates, observed_at=NOW, key_id="k", private_key=key)
    write_local(document, tmp_path)
    with pytest.raises(PublishError, match="write-once"):
        write_local(document, tmp_path)


def test_load_previous_rates_from_a_local_file(tmp_path: Path) -> None:
    artifact = tmp_path / "latest.json"
    artifact.write_text(json.dumps({"rates": [{"machine_type": "n2-standard-2", "region": "us-central1"}]}))
    assert load_previous_rates(str(artifact)) == [{"machine_type": "n2-standard-2", "region": "us-central1"}]


def test_load_previous_rates_is_empty_on_first_run() -> None:
    assert load_previous_rates(None) == []
    assert load_previous_rates("") == []
