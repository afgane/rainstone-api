"""GCP Cloud Billing Catalog fetch, mapping and signing for Rainstone's price catalog.

This package backs ``scripts/publish_gcp_catalog.py``. It has no dependency on
a running Rainstone deployment: it fetches official prices, maps them onto a
fixed machine-shape registry, and produces a signed catalog artifact that
``backend/rainstone/catalog.py`` can validate and import.
"""
