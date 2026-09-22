"""Catalog artifacts from a feed are untrusted until a signature verifies."""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rainstone.catalog import CatalogError, canonical_content, parse_trusted_keys, validate

RATES = [
    {
        "provider": "gcp",
        "region": "us-central1",
        "purchase_model": "on_demand",
        "machine_type": "n2-standard-2",
        "hourly_rate": "0.097118",
    }
]


def artifact(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "catalog_id": "signed-catalog-1",
        "observed_at": "2026-09-19T13:32:03Z",
        "currency": "USD",
        "source_urls": ["https://example.invalid/pricing"],
        "rates": RATES,
    }
    payload.update(overrides)
    return payload


def sign(payload: dict, key: Ed25519PrivateKey, key_id: str = "release-2026") -> bytes:
    signature = key.sign(canonical_content(payload))
    signed = {
        **payload,
        "signature": {
            "key_id": key_id,
            "algorithm": "ed25519",
            "signature": base64.b64encode(signature).decode(),
        },
    }
    return json.dumps(signed).encode()


def trusted(key: Ed25519PrivateKey, key_id: str = "release-2026") -> dict[str, bytes]:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return parse_trusted_keys(f"{key_id}:{base64.b64encode(public).decode()}")


def test_a_correctly_signed_artifact_is_accepted() -> None:
    key = Ed25519PrivateKey.generate()
    catalog = validate(
        sign(artifact(), key),
        source="https://example.invalid/latest.json",
        require_signature=True,
        trusted_keys=trusted(key),
    )
    assert catalog.signature_verified is True
    assert catalog.signature_key_id == "release-2026"
    assert catalog.provenance["signature_verified"] is True


def test_an_unsigned_artifact_is_refused_when_a_signature_is_required() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(CatalogError, match="verified catalog signature is required"):
        validate(
            json.dumps(artifact()).encode(),
            source="feed",
            require_signature=True,
            trusted_keys=trusted(key),
        )


def test_a_made_up_key_id_without_a_signature_is_refused() -> None:
    key = Ed25519PrivateKey.generate()
    payload = artifact()
    payload["signature"] = {"key_id": "untrusted-no-signature"}
    with pytest.raises(CatalogError, match="name a key_id and carry a signature"):
        validate(
            json.dumps(payload).encode(),
            source="feed",
            require_signature=True,
            trusted_keys=trusted(key),
        )


def test_an_artifact_signed_by_an_untrusted_key_is_refused() -> None:
    publisher = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    with pytest.raises(CatalogError, match="untrusted key"):
        validate(
            sign(artifact(), attacker, key_id="attacker"),
            source="feed",
            require_signature=True,
            trusted_keys=trusted(publisher),
        )


def test_altered_prices_break_the_signature() -> None:
    key = Ed25519PrivateKey.generate()
    signed = json.loads(sign(artifact(), key))
    signed["rates"][0]["hourly_rate"] = "0.000001"
    with pytest.raises(CatalogError, match="does not verify"):
        validate(
            json.dumps(signed).encode(),
            source="feed",
            require_signature=True,
            trusted_keys=trusted(key),
        )


def test_a_self_declared_digest_is_not_authenticity() -> None:
    """An attacker controls both the content and its declared digest."""
    key = Ed25519PrivateKey.generate()
    payload = artifact()
    payload["signature"] = {
        "key_id": "release-2026",
        "content_digest": __import__("hashlib")
        .sha256(canonical_content(payload))
        .hexdigest(),
    }
    with pytest.raises(CatalogError, match="name a key_id and carry a signature"):
        validate(
            json.dumps(payload).encode(),
            source="feed",
            require_signature=True,
            trusted_keys=trusted(key),
        )


def test_rotation_accepts_either_trusted_key() -> None:
    retiring = Ed25519PrivateKey.generate()
    replacement = Ed25519PrivateKey.generate()
    keys = {**trusted(retiring, "release-2025"), **trusted(replacement, "release-2026")}
    for key, key_id in ((retiring, "release-2025"), (replacement, "release-2026")):
        catalog = validate(
            sign(artifact(), key, key_id=key_id),
            source="feed",
            require_signature=True,
            trusted_keys=keys,
        )
        assert catalog.signature_key_id == key_id
    dropped = trusted(replacement, "release-2026")
    with pytest.raises(CatalogError, match="untrusted key"):
        validate(
            sign(artifact(), retiring, key_id="release-2025"),
            source="feed",
            require_signature=True,
            trusted_keys=dropped,
        )


def test_a_signed_artifact_needs_pinned_keys_to_be_accepted_at_all() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(CatalogError, match="pins no trusted keys"):
        validate(sign(artifact(), key), source="feed", trusted_keys={})


def test_trusted_key_configuration_is_validated() -> None:
    with pytest.raises(CatalogError, match="key_id:base64"):
        parse_trusted_keys("not-a-pair")
    with pytest.raises(CatalogError, match="not an Ed25519 public key"):
        parse_trusted_keys("short:" + base64.b64encode(b"tiny").decode())


def test_a_feed_refresh_rejects_unsigned_content_by_default(monkeypatch) -> None:
    """The minimal feed configuration must not accept unsigned artifacts."""
    from pathlib import Path

    from rainstone import catalog as catalog_module

    key = Ed25519PrivateKey.generate()
    unsigned = json.dumps(artifact()).encode()

    class Response:
        def read(self) -> bytes:
            return unsigned

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(catalog_module.urllib.request, "urlopen", lambda *_, **__: Response())
    with pytest.raises(CatalogError, match="required but absent"):
        catalog_module.fetch("https://feed.invalid/latest.json", trusted_keys=trusted(key))

    class FailedSession:
        """`refresh` must keep the working catalog rather than clearing it."""

        def __init__(self) -> None:
            self.rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

    session = FailedSession()
    monkeypatch.setattr(
        catalog_module, "active_catalog", lambda _: type("Version", (), {"catalog_id": "bundled"})()
    )
    result = catalog_module.refresh(
        session,
        url="https://feed.invalid/latest.json",
        bundled_path=Path("catalog/gcp-2026-09-19.json"),
        trusted_keys=trusted(key),
    )
    assert result["status"] == "last_known_good"
    assert result["catalog_id"] == "bundled"
    assert session.rolled_back is True
    assert "required but absent" in result["error"]
