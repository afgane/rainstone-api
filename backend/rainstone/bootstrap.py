"""On-VM initialization.

Bootstrap runs once per install or upgrade, with installation-time database
privileges that the web and collector processes never receive. It provisions a
dedicated read-only Galaxy role with column-level grants, seeds this instance's
identity and binds it to the source database, records the baseline accounting
policy, and resolves the shared Galaxy account this deployment reports on.

It never asks for an operator kubeconfig, cloud token or Galaxy superuser
credential, and it never grants Rainstone's own database superuser rights.
"""

import json
import secrets
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from rainstone.adapters.galaxy_db import (
    REQUIRED_TABLES,
    SOURCE_IDENTITY_SCHEMA,
    SOURCE_IDENTITY_TABLE,
    GalaxyCapabilities,
    discover_capabilities,
)
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


def enroll_source_identity(connection: Connection, *, role: str) -> tuple[str, bool]:
    """Give this source database a durable identity the reader can read.

    The identity lives in the source database, so it survives upgrades and
    restores of that database and travels with a restored copy. A different
    database, even with an identical schema, gets a different identity and is
    therefore refused until an operator enrolls it deliberately.
    """
    quoted_role = _quote(role)
    schema = _quote(SOURCE_IDENTITY_SCHEMA)
    table = f"{schema}.{_quote(SOURCE_IDENTITY_TABLE)}"
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    connection.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "identity uuid PRIMARY KEY, "
            "enrolled_at timestamptz NOT NULL DEFAULT now(), "
            "note text)"
        )
    )
    existing = connection.execute(
        text(f"SELECT identity::text AS identity FROM {table} ORDER BY enrolled_at LIMIT 1")
    ).scalar()
    created = existing is None
    if created:
        existing = str(uuid.uuid4())
        connection.execute(
            text(f"INSERT INTO {table} (identity, note) VALUES (:identity, :note)"),
            {
                "identity": existing,
                "note": "Rainstone source-lifetime identity; do not copy between databases.",
            },
        )
    connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {quoted_role}"))
    connection.execute(text(f"GRANT SELECT ON {table} TO {quoted_role}"))
    return existing, created


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


SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def write_kubernetes_secret(
    target: str,
    value: str,
    *,
    service_account_dir: Path = SERVICE_ACCOUNT_DIR,
    api_server: str = "https://kubernetes.default.svc",
) -> dict:
    """Store the scoped reader DSN where the collector can mount it.

    Initialization runs in the cluster with a role that may write exactly this
    Secret, so an operator never has to copy a credential by hand.
    """
    name, _, key = target.partition("/")
    key = key or "dsn"
    namespace = (service_account_dir / "namespace").read_text().strip()
    token = (service_account_dir / "token").read_text().strip()
    ca_path = service_account_dir / "ca.crt"
    context = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else None
    url = f"{api_server}/api/v1/namespaces/{namespace}/secrets/{name}"
    patch = json.dumps({"stringData": {key: value}}).encode()

    def call(method: str, endpoint: str, body: bytes, content_type: str) -> int:
        request = urllib.request.Request(
            endpoint,
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
            return response.status

    try:
        call("PATCH", url, patch, "application/strategic-merge-patch+json")
        created = False
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        body = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "type": "Opaque",
                "stringData": {key: value},
            }
        ).encode()
        call(
            "POST",
            f"{api_server}/api/v1/namespaces/{namespace}/secrets",
            body,
            "application/json",
        )
        created = True
    return {"secret": name, "key": key, "namespace": namespace, "created": created}


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
    source_identity: str,
    replace_source: bool = False,
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

    now = datetime.now(UTC)
    binding = session.scalar(select(SourceBinding).where(SourceBinding.tenant_id == tenant_id))
    if binding is None:
        binding = SourceBinding(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            instance_uuid=uuid.uuid4(),
            source_kind="galaxy_db",
            source_identity=source_identity,
            bound_at=now,
            enrolled_at=now,
        )
        session.add(binding)
    elif binding.source_identity != source_identity:
        if not replace_source:
            # A different source database must not inherit this instance's
            # identity, and with it another database's job IDs.
            raise RuntimeError(
                "this instance is bound to a different source database "
                f"({binding.source_identity}); re-run bootstrap with --replace-source to "
                "enroll the new source deliberately, or seed a new instance identity"
            )
        binding.source_identity = source_identity
        binding.enrolled_at = now
    binding.schema_fingerprint = capabilities.schema_fingerprint
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
        "source_identity": binding.source_identity,
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
    replace_source: bool = False,
    secret_target: str | None = None,
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
        source_identity, identity_created = enroll_source_identity(connection, role=reader_role)
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
            source_identity=source_identity,
            replace_source=replace_source,
        )
    if dsn_output and credential.dsn:
        dsn_output.parent.mkdir(parents=True, exist_ok=True)
        dsn_output.write_text(credential.dsn)
        dsn_output.chmod(0o600)
    secret_result = None
    if secret_target and credential.dsn:
        secret_result = write_kubernetes_secret(secret_target, credential.dsn)
    with Session(application_engine) as session:
        # Record what initialization could verify, in its own credentials'
        # context, so the web process can report it later.
        from rainstone.doctor import record_report, run_checks

        report = run_checks(session, settings, context="bootstrap")
        record_report(session, stable_id("tenant", settings.tenant_slug), report)
        session.commit()
    return {
        **seeded,
        "source_identity_created": identity_created,
        "reader_role": credential.role,
        "reader_credential_written": bool(dsn_output and credential.dsn),
        "reader_credential_secret": secret_result,
        "reader_credential_rotated": credential.rotated,
        "schema_compatible": capabilities.compatible,
        "schema_missing": list(capabilities.missing),
    }
