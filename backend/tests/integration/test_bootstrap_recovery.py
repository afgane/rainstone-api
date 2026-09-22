"""The optional reader provisioning must recover from an interruption.

Provisioning the reader role commits to the source database, while publishing
the credential writes to Kubernetes. A failure between them must leave a state
the next ordinary run repairs, because the collector cannot work without a
credential it can read.

This is the non-default mode. The default profile reads Galaxy with a
credential that already exists and provisions nothing.
"""

import uuid

import pytest
from rainstone import bootstrap as bootstrap_module
from rainstone.bootstrap import bootstrap
from rainstone.config import Settings
from rainstone.db import engine
from rainstone.ingestion import stable_id
from rainstone.models import Tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

from .test_galaxy_collection import SCHEMA, source_engine  # noqa: F401 - fixture reuse

ROLE = "rainstone_recovery_reader"


class FakeSecretStore:
    """Stands in for the Kubernetes Secret the chart mounts into the collector."""

    def __init__(self) -> None:
        self.value: str | None = None
        self.writes = 0
        self.fail_next = False

    def write(self, target: str, value: str, **_: object) -> dict:
        self.writes += 1
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated Secret API failure")
        self.value = value
        return {"secret": target, "created": False}

    def read(self, target: str, **_: object) -> str | None:
        return self.value


@pytest.fixture()
def secret_store(monkeypatch) -> FakeSecretStore:
    store = FakeSecretStore()
    monkeypatch.setattr(bootstrap_module, "write_kubernetes_secret", store.write)
    monkeypatch.setattr(bootstrap_module, "read_kubernetes_secret", store.read)
    return store


@pytest.fixture()
def instance(source_engine):  # noqa: F811 - fixture injection
    settings = Settings(
        auth_mode="development", demo_data=True, tenant_slug=f"recover-{uuid.uuid4().hex[:8]}"
    )

    def remove_role() -> None:
        with engine.begin() as connection:
            if connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": ROLE}
            ).scalar():
                connection.execute(text(f'DROP OWNED BY "{ROLE}" CASCADE'))
                connection.execute(text(f'DROP ROLE "{ROLE}"'))

    remove_role()
    yield settings
    remove_role()
    with Session(engine) as session:
        tenant = session.get(Tenant, stable_id("tenant", settings.tenant_slug))
        if tenant is not None:
            session.delete(tenant)
            session.commit()


def admin_dsn() -> str:
    """The Galaxy-shaped tables live in their own schema in this test database."""
    base = engine.url.render_as_string(hide_password=False)
    return f"{base}?options=-csearch_path%3D{SCHEMA}%2Cpublic"


def run(settings: Settings) -> dict:
    return bootstrap(
        settings,
        admin_database_url=admin_dsn(),
        reader_role=ROLE,
        secret_target="rainstone-source/dsn",
    )


def test_a_failed_secret_write_is_repaired_by_the_next_run(instance, secret_store) -> None:
    secret_store.fail_next = True
    with pytest.raises(OSError, match="simulated Secret API failure"):
        run(instance)
    assert secret_store.value is None

    # The role now exists without a published credential; an ordinary retry
    # must rotate and publish rather than reporting success with nothing stored.
    result = run(instance)
    assert result["reader_credential_republished"] is True
    assert result["reader_credential_secret"] is not None
    assert secret_store.value and secret_store.value.startswith("postgresql+psycopg://")


def test_a_lost_secret_is_republished(instance, secret_store) -> None:
    run(instance)
    published = secret_store.value
    secret_store.value = None  # the Secret was deleted out from under the release

    result = run(instance)
    assert result["reader_credential_republished"] is True
    assert secret_store.value is not None
    assert secret_store.value != published


def test_a_stale_secret_is_replaced_with_a_working_credential(instance, secret_store) -> None:
    run(instance)
    secret_store.value = secret_store.value.replace(ROLE, "rainstone_missing_role")

    result = run(instance)
    assert result["reader_credential_republished"] is True
    assert ROLE in secret_store.value


def test_a_healthy_installation_does_not_rotate_on_every_run(instance, secret_store) -> None:
    run(instance)
    published, writes = secret_store.value, secret_store.writes

    result = run(instance)
    assert result["reader_credential_republished"] is False
    assert secret_store.value == published
    assert secret_store.writes == writes


def test_initialization_does_not_report_success_without_a_usable_credential(
    instance, secret_store, monkeypatch
) -> None:
    monkeypatch.setattr(bootstrap_module, "credential_works", lambda *_, **__: False)
    with pytest.raises(RuntimeError, match="cannot open a session"):
        run(instance)
