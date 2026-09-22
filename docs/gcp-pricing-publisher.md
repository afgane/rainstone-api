# GCP pricing publisher

Publishes the price catalog Rainstone consumes: T2D standard and N2
standard/highmem/highcpu, on-demand, USD compute prices only, across their
priced regions. It is a release-side maintainer tool, not part of a customer
deployment, and needs no server, database or Galaxy connection.

- `scripts/publish_gcp_catalog.py`: fetch, normalize, validate, sign, write.
- `scripts/gcp_pricing/`: the fetch/mapping/output helpers it's built from.
- `scripts/verify_published_catalog.py`: re-verify a local or published
  artifact against a trusted key; used by the workflow and by hand.
- `catalog/gcp-machine-shapes.json`: the 37 supported machine shapes. No
  regional price lives here; reverify it against the [official
  specifications](https://docs.cloud.google.com/compute/docs/general-purpose-machines)
  when adding shapes.
- `.github/workflows/publish-prices.yml`: runs the above monthly.

Excluded from this scope: Spot/preemptible, custom machines, other
families/providers, commitments/discounts, premium OS licenses, GPU, disk and
network. Unknown combinations stay unpriced rather than guessed.

## How matching works

The [Cloud Billing Catalog
API](https://docs.cloud.google.com/billing/v1/how-tos/catalog-api) is queried
for Compute Engine's complete USD SKU listing. A SKU is selected as a T2D or
N2 CPU/RAM component only when its category says `resourceFamily: Compute`
and `usageType: OnDemand`, its description carries none of a short excluded-
terms list (`Custom`, `Sole Tenancy`, `Premium`, `Spot`, `Preemptible`,
`Commitment`, `Reserved`, `Extreme`), and its description matches an anchored
prefix (`"N2 Instance Core running in"`, `"T2D AMD Instance Ram running
in"`, ...). This is deliberately layered rather than a single check, because
categories alone group lookalike families (N2D, T2A, N2 Custom) together with
the real ones.

Each family's CPU and RAM rate is shared by every variant (standard, highmem,
highcpu): GCP prices by provisioned vCPU and GiB, not by variant name. A
region is only priced for a family when exactly one CPU SKU and one RAM SKU
match there; an ambiguous or missing pair blocks that one family/region
combination and is logged, never filled from another region's rate.

`us-central1` and `us-east4` must resolve for both families or the run fails
outright. Losing a previously-published family/region combination also fails
the run, unless explicitly acknowledged with
`--acknowledge-regression family:region` (repeatable).

## Running locally

Fixture-only, no network access needed for the test suite (see `make
test-unit`, and `make test-integration` for the DB-backed import + costing
round trip). Fixtures live under `scripts/fixtures/gcp_billing/`.

Against the live API, from inside the `backend` dev container (or any
environment with `requirements.lock` installed and `rainstone` importable):

```console
export GCP_CATALOG_API_KEY=...
export GCP_CATALOG_SIGNING_KEY=...   # base64, 32-byte Ed25519 seed
python scripts/publish_gcp_catalog.py --key-id local-test --dry-run
```

Drop `--dry-run` and add `--output-dir gcp-catalog-output` (the default) to
write `gcp/versions/<catalog_id>.json` and `gcp/latest.json` locally; nothing
here publishes anything. `--previous-catalog <path-or-url>` runs the
regression check against a prior artifact.

## One-time maintainer setup

1. A Google Cloud project used only for Catalog API access (never a customer
   project): enable the Cloud Billing API there, and create an API key
   restricted to it.
2. An Ed25519 signing keypair:

   ```console
   python - <<'PY'
   import base64
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
   key = Ed25519PrivateKey.generate()
   print("signing key (secret): ", base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode())
   print("public key (not secret):", base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
   PY
   ```

   Pick a `key_id` for it (e.g. `release-2026`). The public key and `key_id`
   are bundled with Rainstone (`RAINSTONE_CATALOG_TRUSTED_KEYS`,
   `backend/rainstone/config.py`) so installations need not configure either.

   **Status (2026-09-22):** done. The live key is `release-202609`, feed
   `https://afgane.github.io/rainstone-api/gcp/latest.json`, both bundled as
   `backend/rainstone/config.py` defaults. Rotating the key later means
   publishing with the new key while the old one is still listed in
   `RAINSTONE_CATALOG_TRUSTED_KEYS` (comma-separated), then dropping the old
   one from the bundled default once installations have picked up a build
   signed with the new key.
3. In the repository's GitHub settings:
   - An environment named `gcp-pricing-publisher`, holding secrets
     `GCP_CATALOG_API_KEY` and `GCP_CATALOG_SIGNING_KEY`, and variables
     `CATALOG_KEY_ID` and `CATALOG_PUBLIC_KEY` (the values from step 2).
     Restrict it to the default branch.
   - Enable Pages, source "GitHub Actions".
   - The application's `RAINSTONE_CATALOG_FEED_URL` should point at
     `https://<owner>.github.io/<repo>/gcp/latest.json` (the workflow assumes
     the same URL by default for its own regression check; set the
     `CATALOG_FEED_URL` repository variable to override it, e.g. for a custom
     domain).

No new VM, subscription service, or user-side account is needed. These are
the credentials that operate the monthly workflow; the script itself is
implemented and tested against fixtures without any of them.

## Monthly workflow

`.github/workflows/publish-prices.yml` runs on `17 6 1 * *` UTC and on manual
dispatch, in one concurrency group. It never runs on a pull request, so the
`gcp-pricing-publisher` environment's secrets are never exposed to PR-triggered
code. Two jobs:

1. **publish** (the `gcp-pricing-publisher` environment): fetch, sign, write
   locally, re-verify the signed output with
   `scripts/verify_published_catalog.py` against the bundled public key, then
   merge the new `gcp/versions/<catalog_id>.json` and `gcp/latest.json` into
   the `gcp-pricing-archive` branch (created as an orphan branch on first
   run). A rerun that reproduces identical bytes for the same `catalog_id` is
   a no-op commit, so an interrupted publish can simply be rerun.
2. **deploy** (the standard `github-pages` environment): checks out
   `gcp-pricing-archive` and deploys it to Pages as-is, so prior versions stay
   reachable under `gcp/versions/`.

Any failure in step 1 (fetch, mapping, required-region check, regression
check, or signature re-verification) stops the workflow before step 2 ever
runs, so a failed or partial run never replaces the live `latest.json`.

## First live acceptance

Implementation and fixture tests are not evidence the SKU mapping is correct
against the *live* catalog. Before trusting a real run:

1. Run the publisher with the maintainer credential (`--dry-run` first).
2. Review the selected SKU IDs and descriptions in each rate's `provenance`.
3. Compare representative T2D and N2 variants, in both `us-central1` and
   `us-east4`, against the [official pricing
   page](https://cloud.google.com/products/compute/pricing/general-purpose).
4. Archive that evidence (outside version control; see `ea-no-commit/`
   conventions), then run the workflow for real and confirm
   `scripts/verify_published_catalog.py <feed-url> --key-id ... --public-key
   ...` against an anonymous (unauthenticated) download of the published
   `latest.json`.

## What this does not change

The consumer side — monthly refresh, overdue-startup and manual checks,
last-known-good fallback, the 45-day missed-update indicator, and signature
enforcement on any downloaded artifact — already exists in
`backend/rainstone/catalog.py` and is covered by the existing unit and
integration suites (`test_catalog_signature.py`, `test_catalog_import.py`).
This publisher produces the artifact those tests already know how to consume;
it does not change job costing, which continues to use the existing qualified
fallback for jobs older than any retained snapshot.
