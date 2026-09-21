"""Catalog import, activation and offline behavior."""

import json
from pathlib import Path

import pytest
from rainstone.catalog import CatalogError, active_catalog, coverage, import_catalog, load_file, refresh
from rainstone.db import engine
from rainstone.models import CatalogVersion, PriceVersion
from sqlalchemy import select
from sqlalchemy.orm import Session

BUNDLED = Path("catalog/gcp-us-central1-2026-09-19.json")


@pytest.fixture()
def session() -> Session:
    with Session(engine) as value:
        yield value
        value.rollback()


@pytest.fixture()
def published(tmp_path) -> Path:
    """A separate published artifact, so committed tests leave fixture prices alone."""
    payload = json.loads(BUNDLED.read_text())
    payload["catalog_id"] = "test-published-catalog"
    payload["observed_at"] = "2026-09-20T00:00:00Z"
    artifact = tmp_path / "published.json"
    artifact.write_text(json.dumps(payload))
    return artifact


def test_import_activates_one_catalog_and_records_provenance(session) -> None:
    result = import_catalog(session, load_file(BUNDLED))
    session.flush()
    assert result["rates"] == 4
    active = active_catalog(session)
    assert active.catalog_id == result["catalog_id"]
    assert active.active is True
    facts = coverage(session)
    assert {item["region"] for item in facts["supported"]} == {"us-central1"}
    assert facts["provenance"]["historical_effective_time_available"] is False
    prices = session.scalars(
        select(PriceVersion).where(PriceVersion.catalog_id == result["catalog_id"])
    ).all()
    assert {price.machine_type for price in prices} >= {"t2d-standard-4", "n2-highmem-4"}


def test_published_catalog_versions_are_immutable(session) -> None:
    catalog = load_file(BUNDLED)
    import_catalog(session, catalog)
    session.flush()
    altered = catalog.__class__(
        catalog_id=catalog.catalog_id,
        schema_version=catalog.schema_version,
        digest="0" * 32,
        observed_at=catalog.observed_at,
        currency=catalog.currency,
        rates=catalog.rates,
        source=catalog.source,
    )
    with pytest.raises(CatalogError, match="immutable"):
        import_catalog(session, altered)


def test_a_failed_refresh_keeps_the_last_known_good_catalog(session, published) -> None:
    import_catalog(session, load_file(published))
    session.commit()
    result = refresh(session, url="http://catalog.invalid/latest.json", bundled_path=published)
    assert result["status"] == "last_known_good"
    assert result["catalog_id"] == "test-published-catalog"
    assert active_catalog(session) is not None


def test_offline_refresh_keeps_serving_the_active_catalog(session, published) -> None:
    import_catalog(session, load_file(published))
    session.commit()
    result = refresh(session, url=None, bundled_path=published)
    assert result["status"] == "current"
    assert active_catalog(session).catalog_id == result["catalog_id"]


def test_a_newer_catalog_replaces_the_active_one(session, tmp_path) -> None:
    import_catalog(session, load_file(BUNDLED))
    session.flush()
    later = json.loads(BUNDLED.read_text())
    later["catalog_id"] = "test-later-catalog"
    later["observed_at"] = "2026-09-20T00:00:00Z"
    artifact = tmp_path / "later.json"
    artifact.write_text(json.dumps(later))
    import_catalog(session, load_file(artifact))
    session.flush()
    assert active_catalog(session).catalog_id == "test-later-catalog"
    previous = session.scalars(
        select(CatalogVersion).where(CatalogVersion.catalog_id != "test-later-catalog")
    ).all()
    assert all(version.active is False for version in previous)
