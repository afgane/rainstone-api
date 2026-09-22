"""Optional provisioning of a dedicated Galaxy reader role.

This is a separate, explicit installation mode, not a startup step. The default
deployment profile reads Galaxy with a credential that already exists and never
changes the source database. Where an installer is permitted to run privileged
DDL, this mode adds a database-enforced boundary instead of the
application-enforced one: a login role with column-level SELECT grants, whose
credential is published to the Secret the collector mounts.

It creates no Rainstone tables in the source database. Enrollment identity lives
in Rainstone's own database; see `rainstone.enrollment`.
"""

import json
import secrets
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from rainstone.adapters.galaxy_db import REQUIRED_TABLES
from rainstone.config import Settings, get_settings

# Column-restricted grants for the tables the adapter reads. `job` is listed
# column by column so the reader cannot read command lines or tool state.
READER_GRANTS: dict[str, tuple[str, ...] | None] = {
    "job": REQUIRED_TABLES["job"],
    "job_state_history": REQUIRED_TABLES["job_state_history"],
    "job_metric_numeric": REQUIRED_TABLES["job_metric_numeric"],
    # `email` is granted so the configured shared account can be matched by the
    # exact address an operator already has. It is matched, never selected.
    "galaxy_user": ("id", "username", "email"),
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


def read_kubernetes_secret(
    target: str,
    *,
    service_account_dir: Path = SERVICE_ACCOUNT_DIR,
    api_server: str = "https://kubernetes.default.svc",
) -> str | None:
    """Read back a published credential, so a retry can reconcile it."""
    import base64

    name, _, key = target.partition("/")
    key = key or "dsn"
    namespace = (service_account_dir / "namespace").read_text().strip()
    token = (service_account_dir / "token").read_text().strip()
    ca_path = service_account_dir / "ca.crt"
    context = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else None
    request = urllib.request.Request(
        f"{api_server}/api/v1/namespaces/{namespace}/secrets/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    encoded = (payload.get("data") or {}).get(key)
    return base64.b64decode(encoded).decode() if encoded else None


def credential_works(dsn: str, *, statement_timeout: str = "10s") -> bool:
    """Does a published credential still open a usable read-only session?"""
    try:
        engine = create_engine(dsn, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{statement_timeout}'")
            connection.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - any failure means it must be republished
        return False


def bootstrap(
    settings: Settings | None = None,
    *,
    admin_database_url: str,
    reader_role: str = "rainstone_reader",
    rotate: bool = False,
    dsn_output: Path | None = None,
    secret_target: str | None = None,
) -> dict:
    """Provision and publish the scoped reader credential.

    Every step is idempotent and recoverable: an interrupted run leaves either a
    working published credential or a state the next run repairs. Enrollment is
    a separate step, so this mode can be skipped entirely.
    """
    settings = settings or get_settings()
    admin_engine = create_engine(admin_database_url, pool_pre_ping=True)

    # Publication is part of provisioning: a credential the collector cannot
    # read is not a completed run. A retry therefore reconciles what was
    # published with what the database holds, rotating when it must, so an
    # interruption between the two writes recovers on the next run.
    published: str | None = None
    if secret_target:
        published = read_kubernetes_secret(secret_target)
    elif dsn_output and dsn_output.exists():
        published = dsn_output.read_text().strip() or None

    with admin_engine.connect() as connection:
        role_exists = bool(
            connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": reader_role}
            ).scalar()
        )
    usable = bool(published) and role_exists and credential_works(published)
    must_rotate = rotate or not role_exists or not usable
    password = secrets.token_urlsafe(32)

    with admin_engine.begin() as connection:
        database = connection.execute(text("SELECT current_database()")).scalar()
        schema = connection.execute(text("SELECT current_schema()")).scalar()
        rotated = provision_reader(
            connection,
            role=reader_role,
            password=password,
            database=database,
            schema=schema,
            rotate=must_rotate,
        )

    dsn = reader_dsn(admin_database_url, reader_role, password) if must_rotate else published
    credential = ReaderCredential(
        role=reader_role,
        password=password if must_rotate else "",
        dsn=dsn or "",
        rotated=bool(rotated),
    )
    if not credential.dsn:
        raise RuntimeError("no scoped reader credential is available to publish; re-run bootstrap")
    republished = must_rotate or not usable
    if dsn_output and republished:
        dsn_output.parent.mkdir(parents=True, exist_ok=True)
        dsn_output.write_text(credential.dsn)
        dsn_output.chmod(0o600)
    secret_result = None
    if secret_target and republished:
        secret_result = write_kubernetes_secret(secret_target, credential.dsn)
        # Verify what was published rather than assuming the write landed.
        stored = read_kubernetes_secret(secret_target)
        if stored != credential.dsn:
            raise RuntimeError(
                f"the scoped reader credential was not stored in {secret_target}; "
                "re-run bootstrap to publish it"
            )
    if not credential_works(credential.dsn):
        raise RuntimeError(
            "the published scoped reader credential cannot open a session; "
            "re-run bootstrap to rotate and republish it"
        )
    return {
        "reader_role": credential.role,
        "reader_credential_written": bool(dsn_output and republished),
        "reader_credential_secret": secret_result,
        "reader_credential_rotated": credential.rotated,
        "reader_credential_republished": republished,
        "source_privilege": "provisioned-reader",
    }
