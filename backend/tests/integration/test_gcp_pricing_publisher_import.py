"""The publisher's own signed output, actually imported and priced.

A fixture-only unit suite proves the pipeline is internally consistent; this
proves the artifact it produces is exactly what `backend/rainstone/catalog.py`
accepts, and that an imported rate prices a representative job the way the
existing cost calculation expects.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from gcp_pricing.mapping import MAPPING_VERSION, build_region_rates
from gcp_pricing.output import build_catalog_document, build_rates
from gcp_pricing.shapes import load_shapes
from rainstone.catalog import active_catalog, import_catalog, parse_trusted_keys, validate
from rainstone.costing import calculate_lifetime
from rainstone.db import engine
from rainstone.models import CapacityRelationship, PriceVersion, ResourceLifetime
from sqlalchemy import select
from sqlalchemy.orm import Session

FIXTURES = Path("scripts/fixtures/gcp_billing")
SHAPES_PATH = Path("catalog/gcp-machine-shapes.json")
NOW = datetime(2026, 9, 22, tzinfo=UTC)


@pytest.fixture()
def session() -> Session:
    with Session(engine) as value:
        yield value
        value.rollback()


def _signed_catalog() -> tuple[dict, str, bytes]:
    skus = json.loads((FIXTURES / "skus_page1.json").read_text())["skus"] + json.loads(
        (FIXTURES / "skus_page2.json").read_text()
    )["skus"]
    region_rates, problems = build_region_rates(skus, now=NOW)
    assert not problems
    shapes = load_shapes(SHAPES_PATH)
    rates = build_rates(shapes, region_rates, observed_at=NOW, mapping_version=MAPPING_VERSION)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    document = build_catalog_document(
        rates,
        observed_at=NOW,
        key_id="test-publisher-key",
        private_key=key,
        catalog_id="test-publisher-import",
    )
    return document, "test-publisher-key", public


def _validated(document: dict, key_id: str, public: bytes):
    trusted = parse_trusted_keys(f"{key_id}:{base64.b64encode(public).decode()}")
    return validate(
        json.dumps(document).encode(), source="local", require_signature=True, trusted_keys=trusted
    )


def test_the_publishers_own_output_imports_and_activates(session) -> None:
    document, key_id, public = _signed_catalog()
    validated = _validated(document, key_id, public)
    result = import_catalog(session, validated)
    session.flush()
    assert result["rates"] == len(document["rates"])
    active = active_catalog(session)
    assert active.catalog_id == "test-publisher-import"
    assert active.signature_verified is True


def test_a_representative_job_prices_from_the_publishers_imported_rate(session) -> None:
    """N2 standard 2-vCPU running one hour in us-central1: an actual imported
    PriceVersion feeding the existing per-lifetime calculation.
    """
    document, key_id, public = _signed_catalog()
    import_catalog(session, _validated(document, key_id, public))
    session.flush()
    price = session.scalars(
        select(PriceVersion).where(
            PriceVersion.catalog_id == "test-publisher-import",
            PriceVersion.machine_type == "n2-standard-2",
            PriceVersion.region == "us-central1",
        )
    ).one()
    assert price.hourly_rate == Decimal("0.097118")

    start = datetime(2026, 9, 22, tzinfo=UTC)
    job = ResourceLifetime(
        provider="gcp",
        resource_key="gce:demo/us-central1-a/900",
        resource_uid="900",
        region="us-central1",
        machine_type="n2-standard-2",
        purchase_model="on_demand",
        capacity_relationship=CapacityRelationship.dedicated,
        observed_start=start,
        observed_end=start + timedelta(hours=1),
        timing_method="provider_billable",
        facts={},
    )
    lines = calculate_lifetime(job, [], price)
    additional = next(line for line in lines if line.basis == "additional")
    assert additional.amount == Decimal("0.097118")
