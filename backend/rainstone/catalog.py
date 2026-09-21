"""Versioned price catalog: validation, import and offline behavior.

Catalog artifacts are immutable and content-addressed. An artifact is validated
before it is trusted, imported atomically, and only then marked active. A failed
refresh leaves the last known good catalog in place, so reporting keeps working
without network access. A pinned historical snapshot is honest about not being
current pricing: its provenance travels into every calculation.

Producing artifacts requires maintainer-side pricing access and is a release
function, not a per-installation credentialed dependency.
"""

import hashlib
import json
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainstone.ingestion import stable_id
from rainstone.models import CatalogVersion, PriceVersion

SCHEMA_VERSION = 2
REQUIRED_FIELDS = {"schema_version", "catalog_id", "observed_at", "currency", "rates", "source_urls"}
REQUIRED_RATE_FIELDS = {"provider", "region", "purchase_model", "machine_type", "hourly_rate"}
SUPPORTED_CURRENCIES = {"USD"}


class CatalogError(ValueError):
    """The artifact cannot be trusted and must not replace a good catalog."""


@dataclass(frozen=True)
class ValidatedCatalog:
    catalog_id: str
    schema_version: int
    digest: str
    observed_at: datetime
    currency: str
    rates: tuple[dict, ...]
    source: str
    signature_key_id: str | None = None
    provenance: dict = field(default_factory=dict)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate(payload: bytes, *, source: str, require_signature: bool = False) -> ValidatedCatalog:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise CatalogError(f"catalog is not valid JSON: {error}") from error
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise CatalogError(f"catalog is missing fields: {', '.join(sorted(missing))}")
    if int(data["schema_version"]) > SCHEMA_VERSION:
        raise CatalogError(
            f"catalog schema {data['schema_version']} is newer than this release supports"
        )
    if data["currency"] not in SUPPORTED_CURRENCIES:
        raise CatalogError(f"unsupported catalog currency: {data['currency']}")
    signature = data.get("signature") or {}
    if require_signature and not signature.get("key_id"):
        raise CatalogError("catalog signature is required but absent")
    declared = signature.get("content_digest")
    keys: set[tuple[str, str, str, str]] = set()
    for rate in data["rates"]:
        absent = REQUIRED_RATE_FIELDS - rate.keys()
        if absent:
            raise CatalogError(f"rate is missing fields: {', '.join(sorted(absent))}")
        try:
            if Decimal(str(rate["hourly_rate"])) <= 0:
                raise CatalogError(f"rate for {rate['machine_type']} is not positive")
        except InvalidOperation as error:
            raise CatalogError(f"rate for {rate['machine_type']} is not a decimal") from error
        key = (rate["provider"], rate["region"], rate["purchase_model"], rate["machine_type"])
        if key in keys:
            raise CatalogError(f"catalog contains duplicate resolver key: {key}")
        keys.add(key)
    content = {key: value for key, value in data.items() if key != "signature"}
    content_digest = _digest(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    )
    if declared and declared != content_digest:
        raise CatalogError("catalog signature digest does not match its content")
    return ValidatedCatalog(
        catalog_id=data["catalog_id"],
        schema_version=int(data["schema_version"]),
        digest=content_digest,
        observed_at=datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00")),
        currency=data["currency"],
        rates=tuple(data["rates"]),
        source=source,
        signature_key_id=signature.get("key_id"),
        provenance={
            "source_urls": data["source_urls"],
            "kind": data.get("kind", "published_snapshot"),
            "historical_effective_time_available": data.get(
                "historical_effective_time_available", False
            ),
            "notes": data.get("notes"),
            "artifact_digest": _digest(payload),
        },
    )


def load_file(path: Path, *, require_signature: bool = False) -> ValidatedCatalog:
    return validate(path.read_bytes(), source=str(path), require_signature=require_signature)


def fetch(url: str, *, timeout: int = 30, require_signature: bool = False) -> ValidatedCatalog:
    """Anonymous download; no account, key or subscription is used."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read()
    return validate(payload, source=url, require_signature=require_signature)


def active_catalog(session: Session) -> CatalogVersion | None:
    return session.scalar(
        select(CatalogVersion)
        .where(CatalogVersion.active.is_(True))
        .order_by(CatalogVersion.imported_at.desc())
    )


def import_catalog(session: Session, catalog: ValidatedCatalog) -> dict:
    """Import prices, then activate the version in the same transaction."""
    existing = session.scalar(
        select(CatalogVersion).where(CatalogVersion.catalog_id == catalog.catalog_id)
    )
    if existing and existing.artifact_digest != catalog.digest:
        raise CatalogError(
            f"catalog {catalog.catalog_id} already exists with different content; "
            "published versions are immutable"
        )
    now = datetime.now(UTC)
    for rate in catalog.rates:
        price_id = stable_id(
            "price", catalog.catalog_id, rate["machine_type"], rate["region"], rate["purchase_model"]
        )
        price = session.get(PriceVersion, price_id)
        if price is None:
            price = PriceVersion(id=price_id)
            session.add(price)
        price.catalog_id = catalog.catalog_id
        price.machine_type = rate["machine_type"]
        price.provider = rate["provider"]
        price.region = rate["region"]
        price.purchase_model = rate["purchase_model"]
        price.currency = catalog.currency
        price.hourly_rate = Decimal(str(rate["hourly_rate"]))
        price.observed_at = catalog.observed_at
        price.effective_from = (
            datetime.fromisoformat(rate["effective_from"].replace("Z", "+00:00"))
            if rate.get("effective_from")
            else None
        )
        price.provenance = {**catalog.provenance, **rate.get("provenance", {})}
    version = existing or CatalogVersion(id=uuid.uuid4(), catalog_id=catalog.catalog_id)
    version.schema_version = catalog.schema_version
    version.artifact_digest = catalog.digest
    version.signature_key_id = catalog.signature_key_id
    version.source = catalog.source[:500]
    version.observed_at = catalog.observed_at
    version.imported_at = now
    version.rate_count = len(catalog.rates)
    version.provenance = catalog.provenance
    if existing is None:
        session.add(version)
    session.flush()
    for other in session.scalars(select(CatalogVersion).where(CatalogVersion.active.is_(True))):
        other.active = False
    version.active = True
    session.flush()
    return {
        "catalog_id": catalog.catalog_id,
        "rates": len(catalog.rates),
        "digest": catalog.digest,
        "source": version.source,
        "replaced": existing is not None,
    }


def refresh(
    session: Session,
    *,
    url: str | None,
    bundled_path: Path | None = None,
    require_signature: bool = False,
) -> dict:
    """Try the feed; fall back to the last known good or bundled catalog."""
    current = active_catalog(session)
    if url:
        try:
            catalog = fetch(url, require_signature=require_signature)
            result = import_catalog(session, catalog)
            session.commit()
            return {**result, "status": "refreshed"}
        except (OSError, CatalogError) as error:
            session.rollback()
            if current is not None:
                return {
                    "status": "last_known_good",
                    "catalog_id": current.catalog_id,
                    "error": str(error),
                }
            if bundled_path is None:
                return {"status": "unavailable", "error": str(error)}
    if current is not None and url is None:
        return {"status": "current", "catalog_id": current.catalog_id}
    if bundled_path is None:
        return {"status": "unavailable", "error": "no catalog source is configured"}
    catalog = load_file(bundled_path, require_signature=require_signature)
    result = import_catalog(session, catalog)
    session.commit()
    return {**result, "status": "bundled"}


def coverage(session: Session) -> dict:
    """Which provider, region, model and shape combinations can be priced."""
    rows = session.execute(
        select(
            PriceVersion.provider,
            PriceVersion.region,
            PriceVersion.purchase_model,
            PriceVersion.machine_type,
            PriceVersion.catalog_id,
        ).order_by(PriceVersion.region, PriceVersion.machine_type)
    ).all()
    version = active_catalog(session)
    return {
        "active_catalog_id": version.catalog_id if version else None,
        "observed_at": version.observed_at.isoformat() if version else None,
        "imported_at": version.imported_at.isoformat() if version else None,
        "signature_key_id": version.signature_key_id if version else None,
        "provenance": version.provenance if version else {},
        "supported": [
            {
                "provider": row.provider,
                "region": row.region,
                "purchase_model": row.purchase_model,
                "machine_type": row.machine_type,
                "catalog_id": row.catalog_id,
            }
            for row in rows
        ],
    }
