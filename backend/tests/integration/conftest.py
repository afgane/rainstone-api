from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rainstone.costing import calculate_tenant
from rainstone.db import engine
from rainstone.ingestion import ingest_fixture
from rainstone.main import app
from rainstone.models import Tenant
from sqlalchemy import select
from sqlalchemy.orm import Session


@pytest.fixture(scope="session", autouse=True)
def seeded_database() -> None:
    with Session(engine) as session:
        ingest_fixture(session, Path("fixtures/phase1.json"))


@pytest.fixture(autouse=True)
def recalculated_after_fact_changes():
    """Recalculate between tests, as the collector does after every batch.

    Report facts advance a generation marker, so a test that writes facts
    directly leaves pinned snapshots stale until something recalculates. Doing
    that here keeps the suite order-independent without weakening the staleness
    contract each test asserts.
    """
    yield
    with Session(engine) as session:
        for tenant_id in session.scalars(select(Tenant.id)).all():
            calculate_tenant(session, tenant_id, reason="test recalculation")
        session.commit()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def headers(user: str, admin: bool = False) -> dict[str, str]:
    result = {"X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": user}
    if admin:
        result["X-Rainstone-Admin"] = "true"
    return result
