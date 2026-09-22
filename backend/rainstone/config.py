from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AUTH_MODES = {"development", "trusted-proxy", "anvil-workspace"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAINSTONE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rainstone:rainstone@localhost:5432/rainstone"
    auth_mode: str = "development"
    demo_data: bool = False
    static_dir: Path = Path("static")
    root_path: str = ""

    # Instance identity. Boot supplies these; they are not discovered from a URL.
    tenant_slug: str = "anvil-demo"
    tenant_display_name: str = "Galaxy deployment"

    # Fixed shared-account reporting scope for the AnVIL workspace profile.
    workspace_owner_source_id: str | None = None
    workspace_infrastructure_visible: bool = True

    # Read-only Galaxy source.
    galaxy_database_url: str | None = None
    galaxy_statement_timeout: str = "30s"
    galaxy_batch_size: int = 500
    galaxy_replay_overlap_seconds: int = 300

    # Execution observation.
    kubernetes_enabled: bool = False
    kubernetes_namespace: str | None = None
    kubernetes_api_server: str | None = None
    kubernetes_watch_seconds: int = 25
    gcp_batch_enabled: bool = False
    gcp_project: str | None = None
    gcp_location: str | None = None
    gcp_max_targets_per_cycle: int = 25
    gcp_enrich_compute: bool = True
    gcp_enrich_logging: bool = True

    # Deployment accounting baseline.
    baseline_policy_version: str | None = None
    baseline_resource_uid: str | None = None
    baseline_machine_type: str | None = None
    baseline_region: str | None = None
    baseline_zone: str | None = None
    baseline_destinations: str = ""
    baseline_runners: str = "local"

    # Collector scheduling.
    collect_interval_seconds: int = 30
    collect_batch_refresh_seconds: int = 30
    collect_terminal_reconcile_seconds: int = 900
    collect_max_backoff_seconds: int = 300
    collector_lease_key: str = "rainstone-collector"

    # Price catalog. Trusted keys are `key_id:base64-ed25519-public-key` pairs;
    # listing several at once is how key rotation works.
    catalog_path: Path = Path("catalog/gcp-us-central1-2026-09-19.json")
    catalog_feed_url: str | None = None
    catalog_refresh_seconds: int = 21600
    catalog_trusted_keys: str = ""
    catalog_require_signature: bool = False

    diagnostics_enabled: bool = True
    diagnostics_interval_seconds: int = 300
    collector_heartbeat_path: Path = Path("/tmp/rainstone-collector.heartbeat")  # noqa: S108

    @property
    def baseline_destination_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.baseline_destinations.split(",") if value.strip())

    @property
    def baseline_runner_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.baseline_runners.split(",") if value.strip())

    @model_validator(mode="after")
    def validate_profile(self) -> "Settings":
        if self.auth_mode not in AUTH_MODES:
            raise ValueError(f"auth_mode must be one of {', '.join(sorted(AUTH_MODES))}")
        if self.auth_mode == "development" and not self.demo_data:
            raise ValueError("development identity requires RAINSTONE_DEMO_DATA=true")
        if self.auth_mode == "anvil-workspace":
            # Failing startup is deliberate: a missing shared-account mapping
            # must never fall back to a fixture identity.
            if not self.workspace_owner_source_id:
                raise ValueError(
                    "anvil-workspace requires RAINSTONE_WORKSPACE_OWNER_SOURCE_ID, "
                    "the Galaxy account this deployment reports on"
                )
            if not self.tenant_slug:
                raise ValueError("anvil-workspace requires RAINSTONE_TENANT_SLUG")
            if self.demo_data:
                raise ValueError("anvil-workspace cannot serve demo data")
        if self.kubernetes_enabled and not self.kubernetes_namespace:
            raise ValueError("Kubernetes observation requires RAINSTONE_KUBERNETES_NAMESPACE")
        if self.gcp_batch_enabled and not (self.gcp_project and self.gcp_location):
            raise ValueError(
                "GCP Batch observation requires RAINSTONE_GCP_PROJECT and RAINSTONE_GCP_LOCATION"
            )
        if self.baseline_policy_version and not self.baseline_resource_uid:
            raise ValueError("A baseline policy requires RAINSTONE_BASELINE_RESOURCE_UID")
        if self.catalog_require_signature and not self.catalog_trusted_keys:
            raise ValueError(
                "RAINSTONE_CATALOG_REQUIRE_SIGNATURE needs RAINSTONE_CATALOG_TRUSTED_KEYS; "
                "a signature cannot be verified without a pinned public key"
            )
        if self.catalog_feed_url and not self.catalog_trusted_keys:
            raise ValueError(
                "a catalog feed needs RAINSTONE_CATALOG_TRUSTED_KEYS: downloaded artifacts are "
                "untrusted until their signature verifies"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
