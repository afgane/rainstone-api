"""Catalog API access: service discovery, pagination, retry and failure handling.

Uses sanitized fixtures under scripts/fixtures/gcp_billing/ rather than the live
API; see docs/gcp-pricing-publisher.md for what first live verification still
requires.
"""

import json
import urllib.error
from pathlib import Path

import pytest
from gcp_pricing import catalog_api
from gcp_pricing.catalog_api import CatalogFetchError, discover_compute_engine_service, list_skus

FIXTURES = Path("scripts/fixtures/gcp_billing")
SERVICES = json.loads((FIXTURES / "services.json").read_text())
PAGE_1 = json.loads((FIXTURES / "skus_page1.json").read_text())
PAGE_2 = json.loads((FIXTURES / "skus_page2.json").read_text())


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch) -> None:
    monkeypatch.setattr(catalog_api.time, "sleep", lambda *_args: None)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def opener_for(pages: dict[str, dict | list]):
    """A fake `opener(request, timeout)` keyed by a marker found in the URL,
    with each entry either a fixed payload or a queue of payloads/exceptions
    consumed in order (for retry/failure scenarios).
    """

    def opener(request, _timeout):
        url = request.full_url
        for marker, response in pages.items():
            if marker in url:
                if isinstance(response, list):
                    outcome = response.pop(0)
                else:
                    outcome = response
                if isinstance(outcome, Exception):
                    raise outcome
                return FakeResponse(outcome)
        raise AssertionError(f"no fixture registered for {url}")

    return opener


def test_service_discovery_finds_compute_engine_by_display_name() -> None:
    service = discover_compute_engine_service("fake-key", opener=opener_for({"/services?": SERVICES}))
    assert service == "services/6F81-5844-456A"


def test_service_discovery_paginates_the_services_listing() -> None:
    first_page = {"services": SERVICES["services"][:1], "nextPageToken": "svc-page-2"}
    pages = opener_for({"pageToken=svc-page-2": SERVICES, "/services?": [first_page]})
    service = discover_compute_engine_service("fake-key", opener=pages)
    assert service == "services/6F81-5844-456A"


def test_service_discovery_fails_closed_when_compute_engine_is_absent() -> None:
    empty = {"services": [{"name": "services/x", "displayName": "Cloud Storage"}], "nextPageToken": ""}
    with pytest.raises(CatalogFetchError, match="Compute Engine"):
        discover_compute_engine_service("fake-key", opener=opener_for({"/services?": empty}))


def test_sku_pagination_collects_every_page() -> None:
    opener = opener_for({"pageToken=page-2-token": PAGE_2, "": PAGE_1})
    skus = list_skus("services/6F81-5844-456A", "fake-key", opener=opener)
    assert len(skus) == len(PAGE_1["skus"]) + len(PAGE_2["skus"])


def test_a_4xx_response_is_not_retried() -> None:
    calls = {"count": 0}

    def opener(_request, _timeout):
        calls["count"] += 1
        raise urllib.error.HTTPError("url", 403, "Forbidden", {}, None)

    with pytest.raises(CatalogFetchError, match="403"):
        list_skus("services/x", "fake-key", opener=opener, retries=3)
    assert calls["count"] == 1


def test_a_5xx_response_is_retried_then_succeeds() -> None:
    responses = [
        urllib.error.HTTPError("url", 503, "Unavailable", {}, None),
        urllib.error.HTTPError("url", 503, "Unavailable", {}, None),
        FakeResponse(PAGE_2),
    ]

    def opener(_request, _timeout):
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    skus = list_skus("services/x", "fake-key", opener=opener, retries=3)
    assert len(skus) == len(PAGE_2["skus"])


def test_retries_are_exhausted_and_reported() -> None:
    def opener(_request, _timeout):
        raise urllib.error.HTTPError("url", 503, "Unavailable", {}, None)

    with pytest.raises(CatalogFetchError, match="after 2 attempts"):
        list_skus("services/x", "fake-key", opener=opener, retries=1)


def test_a_page_missing_the_skus_field_is_rejected_as_incomplete() -> None:
    def opener(_request, _timeout):
        return FakeResponse({"nextPageToken": ""})

    with pytest.raises(CatalogFetchError, match="'skus' field"):
        list_skus("services/x", "fake-key", opener=opener)


def test_an_empty_page_that_claims_more_pages_is_rejected() -> None:
    def opener(_request, _timeout):
        return FakeResponse({"skus": [], "nextPageToken": "still-more"})

    with pytest.raises(CatalogFetchError, match="empty but claimed"):
        list_skus("services/x", "fake-key", opener=opener)


def test_a_repeated_page_token_does_not_loop_forever() -> None:
    def opener(_request, _timeout):
        return FakeResponse({"skus": [{"skuId": "x"}], "nextPageToken": "loop"})

    with pytest.raises(CatalogFetchError, match="looped"):
        list_skus("services/x", "fake-key", opener=opener)


def test_the_api_key_never_appears_in_the_request_url() -> None:
    captured = {}

    def opener(request, _timeout):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        return FakeResponse(SERVICES)

    discover_compute_engine_service("super-secret-key", opener=opener)
    assert "super-secret-key" not in captured["url"]
    assert captured["headers"].get("X-goog-api-key") == "super-secret-key"
