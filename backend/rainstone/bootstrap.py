"""On-VM initialization.

Bootstrap runs once per install or upgrade, with installation-time database
privileges that the web and collector processes never receive. It provisions a
dedicated read-only Galaxy role with column-level grants, seeds this instance's
identity and binds it to the source database, records the baseline accounting
policy, and resolves the shared Galaxy account this deployment reports on.

It never asks for an operator kubeconfig, cloud token or Galaxy superuser
credential, and it never grants Rainstone's own database superuser rights.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from rainstone.adapters.galaxy_db import REQUIRED_TABLES, GalaxyCapabilities, discover_capabilities
from rainstone.config import Settings, get_settings
from rainstone.ingestion import stable_id
from rainstone.models import DeploymentPolicy, Owner, SourceBinding, Tenant

# Column-restricted grants for the tables the adapter reads. `job` is listed
# column by column so the reader cannot read command lines or tool state.
READER_GRANTS: dict[str, tuple[str, ...] | None] = {
    "job": REQUIRED_TABLES["job"],
    "job_state_history": REQUIRED_TABLES["job_state_history"],
    "job_metric_numeric": REQUIRED_TABLES["job_metric_numeric"],
    "galaxy_user": ("id", "username"),
    "history": ("id", "user_id"),
    "workflow_invocation": ("id", "workflow_id", "history_id", "state", "create_time", "update_time"),
    "workflow_invocation_step": REQUIRED_TABLES["workflow_invocation_step"] + ("state",),
    "workflow_invocation_to_subworkflow_invocation_association": None,
    "implicit_collection_jobs_job_association": None,
    "workflow": ("id", "stored_workflow_id", "name"),
}


@dataclass(frozen=True)
class ReaderCredential:
    role: str
    password: str
    dsn: str
    rotated: bool


def _quote(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


def _password_literal(password: str) -> str:
    """Role DDL cannot take bind parameters, so the literal is charset-checked.

    Generated passwords are URL-safe base64, which contains no quote or
    backslash characters.
    """
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    if not password or not set(password) <= allowed:
        raise ValueError("reader password must be URL-safe base64 characters")
    return f"'{password}'"


def provision_reader(
    connection: Connection,
    *,
    role: str,
    password: str,
    database: str,
    schema: str = "public",
    rotate: bool = False,
) -> bool:
    """Create or rotate the scoped reader role. Returns True when rotated."""
    quoted_role = _quote(role)
    exists = connection.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    ).scalar()
    if exists and not rotate:
        rotated = False
    else:
        statement = "ALTER ROLE" if exists else "CREATE ROLE"
        connection.execute(
            text(
                f"{statement} {quoted_role} WITH LOGIN PASSWORD {_password_literal(password)} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        )
        rotated = bool(exists)
    connection.execute(text(f"GRANT CONNECT ON DATABASE {_quote(database)} TO {quoted_role}"))
    connection.execute(text(f"GRANT USAGE ON SCHEMA {_quote(schema)} TO {quoted_role}"))
    for table, columns in READER_GRANTS.items():
        present = connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table"
            ),
            {"schema": schema, "table": table},
        ).scalar()
        if not present:
            continue
        target = f"{_quote(schema)}.{_quote(table)}"
        if columns is None:
            connection.execute(text(f"GRANT SELECT ON {target} TO {quoted_role}"))
            continue
        available = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table"
                ),
                {"schema": schema, "table": table},
            )
        }
        granted = [column for column in columns if column in available]
        column_list = ", ".join(_quote(column) for column in granted)
        connection.execute(text(f"GRANT SELECT ({column_list}) ON {target} TO {quoted_role}"))
    return rotated


def reader_dsn(admin_dsn: str, role: str, password: str) -> str:
    """Rewrite the bootstrap DSN's credentials for the scoped reader role."""
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(admin_dsn)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_shared_account(admin_dsn: str, account: str) -> tuple[str, str]:
    """Map the configured shared Galaxy account to its source owner ID."""
    engine = create_engine(admin_dsn, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        row = connection.execute(
            text(
                "SELECT id::text AS source_id, username FROM galaxy_user "
                "WHERE username = :account OR id::text = :account"
            ),
            {"account": account},
        ).mappings()
        matches = list(row)
    if not matches:
        raise RuntimeError(f"the configured Galaxy account '{account}' does not exist")
    if len(matches) > 1:
        raise RuntimeError(f"the configured Galaxy account '{account}' is ambiguous")
    return matches[0]["source_id"], matches[0]["username"] or account


def seed_instance(
    session: Session,
    settings: Settings,
    capabilities: GalaxyCapabilities,
    *,
    owner_source_id: str,
    owner_label: str,
    descriptor: dict,
) -> dict:
    """Idempotently seed tenant identity, source binding, policy and owner."""
    tenant_id = stable_id("tenant", settings.tenant_slug)
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        tenant = Tenant(
            id=tenant_id,
            slug=settings.tenant_slug,
            display_name=settings.tenant_display_name,
            capabilities={},
        )
        session.add(tenant)
    tenant.display_name = settings.tenant_display_name
    tenant.source_version = capabilities.source_version
    tenant.capabilities = {
        **(tenant.capabilities or {}),
        "galaxy_db": True,
        "kubernetes": settings.kubernetes_enabled,
        "gcp_batch": settings.gcp_batch_enabled,
        "demo": False,
        "workspace_owner_source_id": owner_source_id,
    }
    tenant.synced_at = datetime.now(UTC)

    binding = session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant_id))
    if binding is None:
        binding = SourceBinding(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            instance_uuid=uuid.uuid4(),
            source_kind="galaxy_db",
            source_fingerprint=capabilities.schema_fingerprint,
            bound_at=datetime.now(UTC),
        )
        session.add(binding)
    elif binding.source_fingerprint != capabilities.schema_fingerprint:
        # A replaced source database must not silently inherit this identity.
        raise RuntimeError(
            "this instance is already bound to a different Galaxy source schema; "
            "seed a new instance identity instead of reusing this one"
        )
    binding.source_version = capabilities.source_version
    binding.descriptor = descriptor

    owner_id = stable_id(str(tenant_id), "owner", owner_source_id)
    owner = session.get(Owner, owner_id)
    if owner is None:
        owner = Owner(id=owner_id, tenant_id=tenant_id, source_id=owner_source_id, label=owner_label)
        session.add(owner)
    owner.label = owner_label

    policy_result = None
    if settings.baseline_policy_version and settings.baseline_resource_uid:
        policy_id = stable_id(str(tenant_id), "policy", settings.baseline_policy_version)
        policy = session.get(DeploymentPolicy, policy_id)
        if policy is None:
            policy = DeploymentPolicy(
                id=policy_id, tenant_id=tenant_id, version=settings.baseline_policy_version
            )
            session.add(policy)
            policy.effective_from = datetime.now(UTC)
        policy.baseline_resource_ids = [settings.baseline_resource_uid]
        policy.assumptions = {
            "unchanged_vm_size": True,
            "unchanged_vm_uptime": True,
            "machine_type": settings.baseline_machine_type,
            "region": settings.baseline_region,
            "zone": settings.baseline_zone,
            "destinations": list(settings.baseline_destination_list),
            "runners": list(settings.baseline_runner_list),
            "default_basis": "additional",
        }
        policy.evidence = (
            "Boot-supplied baseline descriptor; placement is verified per observation "
            "before any zero additional-spend statement."
        )
        policy_result = settings.baseline_policy_version

    session.commit()
    return {
        "tenant": settings.tenant_slug,
        "instance_uuid": str(binding.instance_uuid),
        "owner_source_id": owner_source_id,
        "owner_label": owner_label,
        "baseline_policy": policy_result,
        "source_version": capabilities.source_version,
    }


def bootstrap(
    settings: Settings | None = None,
    *,
    admin_database_url: str,
    shared_account: str,
    reader_role: str = "rainstone_reader",
    rotate: bool = False,
    dsn_output: Path | None = None,
    descriptor: dict | None = None,
) -> dict:
    """Provision the reader role, seed identity, and report what to store."""
    settings = settings or get_settings()
    from rainstone.db import engine as application_engine

    admin_engine = create_engine(admin_database_url, pool_pre_ping=True)
    password = secrets.token_urlsafe(32)
    with admin_engine.begin() as connection:
        database = connection.execute(text("SELECT current_database()")).scalar()
        schema = connection.execute(text("SELECT current_schema()")).scalar()
        existing = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": reader_role}
        ).scalar()
        rotated = provision_reader(
            connection,
            role=reader_role,
            password=password,
            database=database,
            schema=schema,
            rotate=rotate or not existing,
        )
    credential = ReaderCredential(
        role=reader_role,
        password=password if (rotated or not existing) else "",
        dsn=reader_dsn(admin_database_url, reader_role, password) if (rotated or not existing) else "",
        rotated=rotated,
    )
    capabilities = discover_capabilities(
        admin_engine, statement_timeout=settings.galaxy_statement_timeout
    )
    owner_source_id, owner_label = resolve_shared_account(admin_database_url, shared_account)
    with Session(application_engine) as session:
        seeded = seed_instance(
            session,
            settings,
            capabilities,
            owner_source_id=owner_source_id,
            owner_label=owner_label,
            descriptor=descriptor or {},
        )
    if dsn_output and credential.dsn:
        dsn_output.parent.mkdir(parents=True, exist_ok=True)
        dsn_output.write_text(credential.dsn)
        dsn_output.chmod(0o600)
    return {
        **seeded,
        "reader_role": credential.role,
        "reader_credential_written": bool(dsn_output and credential.dsn),
        "reader_credential_rotated": credential.rotated,
        "schema_compatible": capabilities.compatible,
        "schema_missing": list(capabilities.missing),
    }
