import pytest
from pydantic import ValidationError
from rainstone.config import Settings
from sqlalchemy.engine import make_url


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


def test_the_source_url_is_built_rather_than_formatted() -> None:
    """A password with URL metacharacters must survive the round trip."""
    settings = Settings(
        auth_mode="development",
        demo_data=True,
        galaxy_db_host="galaxy-postgres-rw",
        galaxy_db_user="galaxydbuser",
        galaxy_db_password="p@ss w/rd:x?y#z",
        galaxy_db_sslmode="require",
    )
    url = settings.galaxy_source_url

    assert url.password == "p@ss w/rd:x?y#z"
    assert url.query["sslmode"] == "require"
    assert make_url(settings.galaxy_dsn).password == "p@ss w/rd:x?y#z"


def test_the_source_endpoint_carries_no_credential() -> None:
    settings = Settings(
        auth_mode="development",
        demo_data=True,
        galaxy_db_host="galaxy-postgres-rw",
        galaxy_db_user="galaxydbuser",
        galaxy_db_password="hunter2",
    )

    assert settings.galaxy_source_endpoint == "galaxy-postgres-rw:5432/galaxy"
    assert "hunter2" not in settings.galaxy_source_endpoint


def test_a_complete_dsn_still_wins_for_standalone_deployments() -> None:
    settings = Settings(
        auth_mode="development",
        demo_data=True,
        galaxy_database_url="postgresql+psycopg://reader:secret@elsewhere:6432/galaxy",
        galaxy_db_host="ignored",
        galaxy_db_user="ignored",
    )

    assert settings.galaxy_source_endpoint == "elsewhere:6432/galaxy"


def test_a_half_configured_source_is_rejected() -> None:
    with pytest.raises(ValidationError, match="RAINSTONE_GALAXY_DB_USER"):
        Settings(auth_mode="development", demo_data=True, galaxy_db_host="galaxy-postgres-rw")


def test_an_unknown_source_privilege_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="galaxy_source_privilege"):
        Settings(auth_mode="development", demo_data=True, galaxy_source_privilege="trust-me")
