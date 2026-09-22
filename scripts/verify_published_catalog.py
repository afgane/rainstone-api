#!/usr/bin/env python3
"""Verify a catalog artifact's signature against a trusted key.

Used by `.github/workflows/publish-prices.yml` right after signing, before
anything is pushed to the pricing archive or deployed to Pages, and by a
maintainer doing the first live acceptance check (see
docs/gcp-pricing-publisher.md): point it at a local file or a public URL and
confirm the signature verifies and coverage looks right.
"""

import argparse
import base64
import binascii
import sys
from pathlib import Path

from rainstone.catalog import CatalogError, fetch, load_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="Local path or http(s) URL to the catalog artifact")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--public-key", required=True, help="Base64-encoded 32-byte Ed25519 public key")
    args = parser.parse_args(argv)

    try:
        material = base64.b64decode(args.public_key, validate=True)
    except binascii.Error:
        print("--public-key is not valid base64", file=sys.stderr)
        return 1
    trusted = {args.key_id: material}

    try:
        if args.source.startswith(("http://", "https://")):
            validated = fetch(args.source, trusted_keys=trusted)
        else:
            validated = load_file(Path(args.source), require_signature=True, trusted_keys=trusted)
    except (CatalogError, OSError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1

    regions = sorted({rate["region"] for rate in validated.rates})
    print(f"catalog_id={validated.catalog_id}")
    print(f"signature_verified={validated.signature_verified} key_id={validated.signature_key_id}")
    print(f"rates={len(validated.rates)} regions={regions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
