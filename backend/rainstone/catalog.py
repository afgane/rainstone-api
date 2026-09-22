"""Versioned price catalog: validation, import and offline behavior.

Catalog artifacts are immutable and content-addressed. An artifact is validated
before it is trusted, imported atomically, and only then marked active. A failed
refresh leaves the last known good catalog in place, so reporting keeps working
without network access. A pinned historical snapshot is honest about not being
current pricing: its provenance travels into every calculation.

An artifact downloaded from a feed is untrusted until an Ed25519 signature over
its canonical content verifies against a release-pinned public key. Several keys
may be trusted at once, which is how key rotation works: publish with the new
key while the old one is still trusted, then drop the old key. A self-declared
digest inside the artifact is an integrity aid only and never establishes
authenticity, because an attacker controls both the content and that digest.

Producing artifacts requires maintainer-side pricing access and is a release
function, not a per-installation credentialed dependency.
"""

import base64
import binascii
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
# Coverage is declared by the publisher rather than inferred from the rows that
# happen to be present: a region the catalog does not claim and a region it
# claims but could not price are different facts, and only the first is a
# complete answer.
COVERAGE_FIELDS = {"regions", "machine_families", "purchase_models"}
SUPPORTED_CURRENCIES = {"USD"}


class CatalogError(ValueError):
    """The artifact cannot be trusted and must not replace a good catalog."""


def parse_trusted_keys(configured: str) -> dict[str, bytes]:
    """Read `key_id:base64-public-key` pairs pinned by the release or operator."""
    trusted: dict[str, bytes] = {}
    for entry in configured.split(","):
        item = entry.strip()
        if not item:
            continue
        key_id, _, encoded = item.partition(":")
        if not key_id or not encoded:
            raise CatalogError(f"trusted catalog key must be 'key_id:base64': {item}")
        try:
            material = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise CatalogError(f"trusted catalog key {key_id} is not valid base64") from error
        if len(material) != 32:
            raise CatalogError(f"trusted catalog key {key_id} is not an Ed25519 public key")
        trusted[key_id.strip()] = material
    return trusted


def verify_signature(content: bytes, signature: dict, trusted: dict[str, bytes]) -> str:
    """Verify an Ed25519 signature over canonical content; return the key ID."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_id = signature.get("key_id")
    encoded = signature.get("signature")
    if not key_id or not encoded:
        raise CatalogError("catalog signature must name a key_id and carry a signature")
    if signature.get("algorithm", "ed25519") != "ed25519":
        raise CatalogError(f"unsupported catalog signature algorithm: {signature['algorithm']}")
    material = trusted.get(key_id)
    if material is None:
        raise CatalogError(f"catalog is signed by untrusted key {key_id}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        raise CatalogError("catalog signature is not valid base64") from error
    try:
        Ed25519PublicKey.from_public_bytes(material).verify(raw, content)
    except InvalidSignature as error:
        raise CatalogError(f"catalog signature does not verify against key {key_id}") from error
    return key_id


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
    signature_verified: bool = False
    provenance: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_content(data: dict) -> bytes:
    """The bytes a publisher signs: the artifact without its signature block."""
    content = {key: value for key, value in data.items() if key != "signature"}
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()


def validate(
    payload: bytes,
    *,
    source: str,
    require_signature: bool = False,
    trusted_keys: dict[str, bytes] | None = None,
) -> ValidatedCatalog:
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
    declared_coverage = data.get("coverage") or {}
    if declared_coverage:
        missing_coverage = COVERAGE_FIELDS - declared_coverage.keys()
        if missing_coverage:
            raise CatalogError(
                f"catalog coverage is missing fields: {', '.join(sorted(missing_coverage))}"
            )
        uncovered = sorted(
            {rate["region"] for rate in data["rates"]} - set(declared_coverage["regions"])
        )
        if uncovered:
            raise CatalogError(
                "catalog prices regions it does not declare coverage for: "
                + ", ".join(uncovered)
            )
    content = canonical_content(data)
    content_digest = _digest(content)
    if declared and declared != content_digest:
        raise CatalogError("catalog digest does not match its content")
    trusted = trusted_keys or {}
    verified_key: str | None = None
    if signature:
        if not trusted:
            raise CatalogError(
                "catalog carries a signature but this deployment pins no trusted keys"
            )
        verified_key = verify_signature(content, signature, trusted)
    if require_signature and verified_key is None:
        raise CatalogError("a verified catalog signature is required but absent")
    return ValidatedCatalog(
        catalog_id=data["catalog_id"],
        coverage=declared_coverage,
        schema_version=int(data["schema_version"]),
        digest=content_digest,
        observed_at=datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00")),
        currency=data["currency"],
        rates=tuple(data["rates"]),
        source=source,
        signature_key_id=verified_key,
        signature_verified=verified_key is not None,
        provenance={
            "source_urls": data["source_urls"],
            "kind": data.get("kind", "published_snapshot"),
            "historical_effective_time_available": data.get(
                "historical_effective_time_available", False
            ),
            "notes": data.get("notes"),
            "artifact_digest": _digest(payload),
            "signature_verified": verified_key is not None,
            "signature_key_id": verified_key,
        },
    )


def load_file(
    path: Path,
    *,
    require_signature: bool = False,
    trusted_keys: dict[str, bytes] | None = None,
) -> ValidatedCatalog:
    return validate(
        path.read_bytes(),
        source=str(path),
        require_signature=require_signature,
        trusted_keys=trusted_keys,
    )


def fetch(
    url: str,
    *,
    timeout: int = 30,
    trusted_keys: dict[str, bytes] | None = None,
) -> ValidatedCatalog:
    """Anonymous download; no account, key or subscription is used.

    A downloaded artifact always needs a verified signature: an attacker who can
    answer the feed can also remove a signature block, so accepting unsigned
    content would make verification optional in practice. Enforcement is not a
    setting here.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read()
    return validate(payload, source=url, require_signature=True, trusted_keys=trusted_keys)


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
    version.signature_verified = catalog.signature_verified
    version.source = catalog.source[:500]
    version.observed_at = catalog.observed_at
    version.imported_at = now
    version.rate_count = len(catalog.rates)
    version.provenance = catalog.provenance
    version.coverage = catalog.coverage
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
    trusted_keys: dict[str, bytes] | None = None,
) -> dict:
    """Try the feed; fall back to the last known good or bundled catalog.

    A downloaded artifact that fails verification never replaces a catalog that
    already works.
    """
    current = active_catalog(session)
    if url:
        try:
            catalog = fetch(url, trusted_keys=trusted_keys)
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
    # The bundled artifact ships inside the release image, so it is trusted by
    # provenance rather than by a feed signature.
    catalog = load_file(bundled_path, trusted_keys=trusted_keys)
    result = import_catalog(session, catalog)
    session.commit()
    return {**result, "status": "bundled"}


def coverage(session: Session) -> dict:
    """What the active catalog claims to cover, and what it can actually price.

    A shape in a claimed region with no rate is a gap in maintained data; a
    shape outside the claimed regions is simply not covered yet. Reporting them
    as one number would hide which of the two an operator is looking at.
    """
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
    claimed = (version.coverage if version else {}) or {}
    priced_regions = sorted({row.region for row in rows})
    return {
        "active_catalog_id": version.catalog_id if version else None,
        "observed_at": version.observed_at.isoformat() if version else None,
        "imported_at": version.imported_at.isoformat() if version else None,
        "signature_key_id": version.signature_key_id if version else None,
        "signature_verified": bool(version.signature_verified) if version else False,
        "provenance": version.provenance if version else {},
        "claimed_coverage": claimed,
        "priced_regions": priced_regions,
        "claimed_regions_without_rates": sorted(
            set(claimed.get("regions", [])) - set(priced_regions)
        ),
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
