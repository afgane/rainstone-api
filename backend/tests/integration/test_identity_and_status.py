"""Identity modes and on-VM diagnostics.

The AnVIL workspace profile reports one fixed tenant and shared Galaxy account.
No request input may select another scope or elevate privileges, and the
diagnostics a viewer can download must not leak connection details.
"""

import json

import pytest
from fastapi.testclient import TestClient
from rainstone.config import Settings, get_settings
from rainstone.main import app

WORKSPACE = Settings(
    auth_mode="anvil-workspace",
    tenant_slug="anvil-demo",
    workspace_owner_source_id="alice",
    workspace_infrastructure_visible=True,
    demo_data=False,
)
PROXY = Settings(auth_mode="trusted-proxy", tenant_slug="anvil-demo", demo_data=False)


@pytest.fixture()
def workspace_client(client: TestClient) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: WORKSPACE
    yield client
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture()
def proxy_client(client: TestClient) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: PROXY
    yield client
    app.dependency_overrides.pop(get_settings, None)


def test_workspace_scope_is_fixed_and_attributed_to_the_shared_account(workspace_client) -> None:
    me = workspace_client.get("/api/me").json()
    assert me["source_id"] == "alice"
    assert me["is_admin"] is False
    assert me["auth_mode"] == "anvil-workspace"
    assert "shared Galaxy account" in me["attribution"]
    assert me["capabilities"]["infrastructure"] is True
    assert me["capabilities"]["users"] is False


def test_client_identity_headers_cannot_change_the_workspace_scope(workspace_client) -> None:
    for header in ("X-Rainstone-User", "X-Rainstone-Tenant", "X-Rainstone-Admin"):
        response = workspace_client.get("/api/summary", headers={header: "bob"})
        assert response.status_code == 403
        assert "fixed workspace scope" in response.json()["detail"]


def test_workspace_viewers_read_only_the_configured_account(workspace_client) -> None:
    jobs = workspace_client.get("/api/jobs?limit=200").json()["items"]
    assert jobs
    assert {item["owner_id"] for item in jobs} == {"alice"}
    # Another owner's work cannot be selected through a query parameter.
    assert workspace_client.get("/api/jobs?owner=bob").json()["items"] == []
    assert workspace_client.get("/api/users").status_code == 403


def test_workspace_infrastructure_is_a_separate_authorized_capability(workspace_client) -> None:
    infrastructure = workspace_client.get("/api/infrastructure")
    assert infrastructure.status_code == 200
    assert "do not apportion" in infrastructure.json()["scope"]
    app.dependency_overrides[get_settings] = lambda: WORKSPACE.model_copy(
        update={"workspace_infrastructure_visible": False}
    )
    denied = workspace_client.get("/api/infrastructure")
    assert denied.status_code == 403
    assert workspace_client.get("/api/summary").json()["baseline_infrastructure_amount"] is None


def test_trusted_proxy_requires_a_forwarded_identity(proxy_client) -> None:
    assert proxy_client.get("/api/summary").status_code == 401
    allowed = proxy_client.get(
        "/api/summary", headers={"X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": "alice"}
    )
    assert allowed.status_code == 200
    escalation = proxy_client.get(
        "/api/summary",
        headers={
            "X-Rainstone-Tenant": "anvil-demo",
            "X-Rainstone-User": "alice",
            "X-Rainstone-Admin": "true",
        },
    )
    assert escalation.status_code == 403
    unknown = proxy_client.get(
        "/api/summary", headers={"X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": "ghost"}
    )
    assert unknown.status_code == 403


def test_status_reports_capabilities_without_leaking_connection_details(workspace_client) -> None:
    status = workspace_client.get("/api/status")
    assert status.status_code == 200
    payload = status.json()
    names = {check["name"] for check in payload["checks"]}
    assert {"application_database", "migration_state", "instance_identity"} <= names
    assert payload["auth_mode"] == "anvil-workspace"
    serialized = json.dumps(payload)
    assert "postgresql+psycopg" not in serialized
    assert "rainstone:rainstone" not in serialized
    assert "password" not in serialized.lower()


def test_diagnostics_download_is_the_same_sanitized_report(workspace_client) -> None:
    response = workspace_client.get("/api/status/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    payload = json.loads(response.text)
    assert payload["tenant"] == "anvil-demo"
    assert "postgresql" not in response.text


def test_catalog_coverage_reports_supported_shapes_and_provenance(workspace_client) -> None:
    coverage = workspace_client.get("/api/catalog").json()
    assert {item["region"] for item in coverage["supported"]} <= {"us-central1"}
    assert all(item["provider"] == "gcp" for item in coverage["supported"])


def test_freshness_exposes_sources_and_recorded_gaps(workspace_client) -> None:
    freshness = workspace_client.get("/api/freshness").json()
    assert {source["source"] for source in freshness["sources"]} >= {"fixture", "price_catalog"}
    assert isinstance(freshness["observation_gaps"], list)
