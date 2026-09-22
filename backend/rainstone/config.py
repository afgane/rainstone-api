from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

AUTH_MODES = {"development", "trusted-proxy", "anvil-workspace"}
SOURCE_PRIVILEGE_MODES = {"application-credential", "provisioned-reader"}


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

    # Read-only Galaxy source. Supply either a complete DSN or the component
    # settings below, which mirror how Galaxy itself keeps its credential: a
    # Secret holding a username and password, plus the PostgreSQL service,
    # database and TLS mode it connects to. Component settings are assembled by
    # SQLAlchemy's URL builder, so passwords and connection options keep their
    # own escaping instead of being pasted into a string.
    galaxy_database_url: str | None = None
    galaxy_db_driver: str = "postgresql+psycopg"
    galaxy_db_host: str | None = None
    galaxy_db_port: int = 5432
    galaxy_db_name: str = "galaxy"
    galaxy_db_user: str | None = None
    galaxy_db_password: str | None = None
    galaxy_db_password_file: Path | None = None
    galaxy_db_sslmode: str | None = None
    galaxy_db_options: str = ""
    # How the source credential is restricted. `application-credential` reads
    # Galaxy with its own application login, which this process confines to
    # read-only transactions, allowlisted statements, a statement timeout and a
    # bounded pool. That is application-enforced behavior, not a privilege
    # boundary the database enforces. `provisioned-reader` names a role created
    # by the optional provisioning mode, where the database enforces it.
    galaxy_source_privilege: str = "application-credential"
    galaxy_statement_timeout: str = "30s"
    galaxy_batch_size: int = 500
    galaxy_replay_overlap_seconds: int = 300

    # Source lifetime. Enrollment lives in Rainstone's own database; replacing
    # the source is an explicit act, never inferred from a changed endpoint.
    source_reenroll: bool = False
    source_deployment_evidence: str = ""

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
    # What Kubernetes calls the baseline VM. A node without a provider ID has
    # no verified VM identity, so its own name is the only thing an observation
    # can be joined on; the numeric instance ID stays the provider identity.
    baseline_node_names: str = ""
    baseline_destinations: str = ""
    baseline_runners: str = "local"

    # Collector scheduling.
    collect_interval_seconds: int = 30
    collect_batch_refresh_seconds: int = 30
    collect_terminal_reconcile_seconds: int = 900
    collect_max_backoff_seconds: int = 300
    collector_lease_key: str = "rainstone-collector"

    # Price catalog. Trusted keys are `key_id:base64-ed25519-public-key` pairs;
    # listing several at once is how key rotation works. The feed URL and key
    # below are Rainstone's own bundled defaults (scripts/publish_gcp_catalog.py,
    # published from github.com/afgane/rainstone-api), so an installation need
    # not configure either; `chart/values.yaml`'s catalog.feedUrl/trustedKeys
    # only need setting to override them, e.g. for a fork's own feed.
    catalog_path: Path = Path("catalog/gcp-2026-09-19.json")
    catalog_feed_url: str | None = "https://afgane.github.io/rainstone-api/gcp/latest.json"
    catalog_refresh_seconds: int = 21600
    catalog_trusted_keys: str = "release-202609:emw6TcnKRKPWuXSUIL4DRUtr9dSFcWwJsXW+Kwy4BsA="
    catalog_require_signature: bool = False

    # Identifies this release's initialization run; readiness waits for it.
    installation_id: str = ""

    diagnostics_enabled: bool = True
    diagnostics_interval_seconds: int = 300
    collector_heartbeat_path: Path = Path("/tmp/rainstone-collector.heartbeat")  # noqa: S108

    @property
    def galaxy_db_secret(self) -> str | None:
        """The source password, from the setting or the mounted Secret file."""
        if self.galaxy_db_password is not None:
            return self.galaxy_db_password
        if self.galaxy_db_password_file is not None:
            return self.galaxy_db_password_file.read_text().strip()
        return None

    @property
    def galaxy_source_url(self) -> URL | None:
        """The resolved source connection, built rather than string-formatted.

        A complete DSN wins when one is configured, so a standalone deployment
        can keep using one. Otherwise the component settings are assembled by
        SQLAlchemy, which escapes the password and keeps connection options in
        the query string where the driver expects them.
        """
        if self.galaxy_database_url:
            return make_url(self.galaxy_database_url)
        if not (self.galaxy_db_host and self.galaxy_db_user):
            return None
        query: dict[str, str] = {}
        if self.galaxy_db_options:
            for option in self.galaxy_db_options.split("&"):
                if not option.strip():
                    continue
                name, _, value = option.partition("=")
                query[name.strip()] = value.strip()
        if self.galaxy_db_sslmode:
            query["sslmode"] = self.galaxy_db_sslmode
        return URL.create(
            self.galaxy_db_driver,
            username=self.galaxy_db_user,
            password=self.galaxy_db_secret,
            host=self.galaxy_db_host,
            port=self.galaxy_db_port,
            database=self.galaxy_db_name,
            query=query,
        )

    @property
    def galaxy_dsn(self) -> str | None:
        url = self.galaxy_source_url
        return url.render_as_string(hide_password=False) if url is not None else None

    @property
    def galaxy_source_endpoint(self) -> str | None:
        """Host, port and database only: safe for logs, findings and output."""
        url = self.galaxy_source_url
        if url is None:
            return None
        host = url.host or "localhost"
        port = url.port or 5432
        return f"{host}:{port}/{url.database or ''}"

    @property
    def baseline_destination_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.baseline_destinations.split(",") if value.strip())

    @property
    def baseline_node_name_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.baseline_node_names.split(",") if value.strip())

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
        if self.galaxy_source_privilege not in SOURCE_PRIVILEGE_MODES:
            raise ValueError(
                "galaxy_source_privilege must be one of "
                f"{', '.join(sorted(SOURCE_PRIVILEGE_MODES))}"
            )
        if not self.galaxy_database_url and (self.galaxy_db_host or self.galaxy_db_user):
            missing = [
                name
                for name, value in (
                    ("RAINSTONE_GALAXY_DB_HOST", self.galaxy_db_host),
                    ("RAINSTONE_GALAXY_DB_USER", self.galaxy_db_user),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "an incomplete Galaxy source connection was configured; "
                    f"missing {', '.join(missing)}"
                )
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
