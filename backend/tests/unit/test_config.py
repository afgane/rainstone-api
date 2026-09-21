import pytest
from pydantic import ValidationError
from rainstone.config import Settings


def test_development_identity_cannot_start_outside_demo_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="development", demo_data=False)


def test_workspace_mode_requires_a_resolved_shared_account() -> None:
    with pytest.raises(ValidationError, match="WORKSPACE_OWNER_SOURCE_ID"):
        Settings(auth_mode="anvil-workspace")


def test_workspace_mode_refuses_demo_data() -> None:
    with pytest.raises(ValidationError, match="demo"):
        Settings(auth_mode="anvil-workspace", workspace_owner_source_id="7", demo_data=True)


def test_unknown_auth_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="auth_mode"):
        Settings(auth_mode="leo-prefix")


def test_execution_observers_require_their_scope() -> None:
    with pytest.raises(ValidationError, match="NAMESPACE"):
        Settings(auth_mode="trusted-proxy", kubernetes_enabled=True)
    with pytest.raises(ValidationError, match="GCP_PROJECT"):
        Settings(auth_mode="trusted-proxy", gcp_batch_enabled=True)


def test_baseline_policy_requires_a_resource_identity() -> None:
    with pytest.raises(ValidationError, match="BASELINE_RESOURCE_UID"):
        Settings(auth_mode="trusted-proxy", baseline_policy_version="v1")
