"""The standalone verifier the publish workflow runs before touching the
archive branch, and a maintainer runs by hand against a live URL.
"""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import verify_published_catalog as verifier
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from gcp_pricing.mapping import ComponentRates, PricePoint
from gcp_pricing.output import build_catalog_document, build_rates
from gcp_pricing.shapes import MachineShape

NOW = datetime(2026, 9, 22, tzinfo=UTC)


def _document(key: Ed25519PrivateKey, key_id: str) -> dict:
    shape = MachineShape("n2-standard-2", "n2", "standard", 2, Decimal("8"), "https://example.invalid")
    cpu = PricePoint("cpu-sku", "N2 Instance Core running in Americas", Decimal("0.031611"), "h", None)
    ram = PricePoint("ram-sku", "N2 Instance Ram running in Americas", Decimal("0.004237"), "GiBy.h", None)
    rates = build_rates(
        (shape,), {("n2", "us-central1"): ComponentRates(cpu, ram)}, observed_at=NOW, mapping_version="v1"
    )
    return build_catalog_document(rates, observed_at=NOW, key_id=key_id, private_key=key)


def test_a_valid_local_artifact_verifies(tmp_path: Path, capsys) -> None:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    artifact = tmp_path / "latest.json"
    artifact.write_text(json.dumps(_document(key, "release-2026")))

    exit_code = verifier.main(
        [str(artifact), "--key-id", "release-2026", "--public-key", base64.b64encode(public).decode()]
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "signature_verified=True" in output


def test_a_wrong_key_id_fails_closed(tmp_path: Path, capsys) -> None:
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    other_public = other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    artifact = tmp_path / "latest.json"
    artifact.write_text(json.dumps(_document(key, "release-2026")))

    exit_code = verifier.main(
        [str(artifact), "--key-id", "release-2026", "--public-key", base64.b64encode(other_public).decode()]
    )
    assert exit_code == 1
    assert "verification failed" in capsys.readouterr().err


def test_a_malformed_public_key_is_rejected(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "latest.json"
    artifact.write_text("{}")
    exit_code = verifier.main([str(artifact), "--key-id", "release-2026", "--public-key", "not-base64!!"])
    assert exit_code == 1
    assert "not valid base64" in capsys.readouterr().err
