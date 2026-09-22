#!/usr/bin/env python3
"""Fetch, normalize, validate, sign and write the GCP T2D/N2 price catalog.

Scope: on-demand, USD compute prices for T2D standard and N2
standard/highmem/highcpu only, across their priced regions. See
`docs/gcp-pricing-publisher.md` for the maintainer runbook.

Runnable locally without publishing anything: `--output-dir` just writes a
directory tree (`gcp/versions/<catalog_id>.json` and `gcp/latest.json`).
Pushing that tree to GitHub Pages is `.github/workflows/publish-prices.yml`'s
job, not this script's.
"""

import argparse
import base64
import binascii
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from gcp_pricing.catalog_api import CatalogFetchError, discover_compute_engine_service, list_skus
from gcp_pricing.mapping import MAPPING_VERSION, build_region_rates
from gcp_pricing.output import (
    PublishError,
    build_catalog_document,
    build_rates,
    check_no_regression,
    check_required_regions,
    load_previous_rates,
    write_local,
)
from gcp_pricing.shapes import DEFAULT_PATH as DEFAULT_SHAPES_PATH
from gcp_pricing.shapes import ShapeRegistryError, load_shapes

DEFAULT_OUTPUT_DIR = Path("gcp-catalog-output")


class CliError(RuntimeError):
    """A configuration problem, not a fetch or mapping failure."""


def load_signing_key(raw: str) -> Ed25519PrivateKey:
    try:
        material = base64.b64decode(raw, validate=True)
    except binascii.Error as error:
        raise CliError("signing key is not valid base64") from error
    if len(material) != 32:
        raise CliError("signing key must be a base64-encoded 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(material)


def run(
    *,
    api_key: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
    shapes_path: Path,
    output_dir: Path | None,
    previous_catalog: str | None,
    acknowledged_regressions: tuple[tuple[str, str], ...] = (),
    timeout: int = 30,
    retries: int = 3,
    now: datetime | None = None,
) -> dict:
    """Fetch, normalize, validate, sign and (unless `output_dir` is None) write.

    Returns the signed catalog document either way, so a dry run can still be
    inspected by the caller.
    """
    observed_at = now or datetime.now(UTC)
    shapes = load_shapes(shapes_path)
    service_name = discover_compute_engine_service(api_key, timeout=timeout, retries=retries)
    skus = list_skus(service_name, api_key, timeout=timeout, retries=retries)
    region_rates, problems = build_region_rates(skus, now=observed_at)
    for problem in problems:
        print(f"gcp_pricing: {problem}", file=sys.stderr)
    check_required_regions(region_rates)
    rates = build_rates(shapes, region_rates, observed_at=observed_at, mapping_version=MAPPING_VERSION)
    previous_rates = load_previous_rates(previous_catalog, timeout=timeout)
    check_no_regression(previous_rates, rates, acknowledged=acknowledged_regressions)
    document = build_catalog_document(
        rates, observed_at=observed_at, key_id=key_id, private_key=private_key
    )
    if output_dir is not None:
        version_path, latest_path = write_local(document, output_dir)
        print(f"wrote {version_path}")
        print(f"wrote {latest_path}")
    return document


def _parse_acknowledged(values: list[str]) -> tuple[tuple[str, str], ...]:
    parsed = []
    for value in values:
        family, _, region = value.partition(":")
        if not family or not region:
            raise CliError(f"--acknowledge-regression must be family:region, got {value!r}")
        parsed.append((family, region))
    return tuple(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key-env", default="GCP_CATALOG_API_KEY", help="Env var holding the Catalog API key")
    parser.add_argument(
        "--signing-key-env",
        default="GCP_CATALOG_SIGNING_KEY",
        help="Env var holding the base64 Ed25519 signing key seed",
    )
    parser.add_argument("--key-id", required=True, help="key_id this catalog's signature will carry")
    parser.add_argument("--shapes-path", type=Path, default=DEFAULT_SHAPES_PATH)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Write gcp/versions/<catalog_id>.json and gcp/latest.json under here",
    )
    parser.add_argument(
        "--previous-catalog",
        default=None,
        help="Path or URL to the last published latest.json, checked for coverage regression",
    )
    parser.add_argument(
        "--acknowledge-regression",
        action="append",
        default=[],
        metavar="FAMILY:REGION",
        help="Explicitly accept losing coverage for family:region (repeatable)",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Fetch, map and sign, but write nothing")
    args = parser.parse_args(argv)

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise CliError(f"{args.api_key_env} is not set")
    signing_key_raw = os.environ.get(args.signing_key_env)
    if not signing_key_raw:
        raise CliError(f"{args.signing_key_env} is not set")
    private_key = load_signing_key(signing_key_raw)

    try:
        document = run(
            api_key=api_key,
            key_id=args.key_id,
            private_key=private_key,
            shapes_path=args.shapes_path,
            output_dir=None if args.dry_run else args.output_dir,
            previous_catalog=args.previous_catalog,
            acknowledged_regressions=_parse_acknowledged(args.acknowledge_regression),
            timeout=args.timeout,
            retries=args.retries,
        )
    except (CatalogFetchError, ShapeRegistryError, PublishError, CliError) as error:
        print(f"gcp_pricing: {error}", file=sys.stderr)
        return 1
    print(
        f"catalog_id={document['catalog_id']} rates={len(document['rates'])} "
        f"regions={document['coverage']['regions']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
