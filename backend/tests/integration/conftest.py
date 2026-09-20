from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rainstone.db import engine
from rainstone.ingestion import ingest_fixture
from rainstone.main import app
from sqlalchemy.orm import Session


@pytest.fixture(scope="session", autouse=True)
def seeded_database() -> None:
    with Session(engine) as session:
        ingest_fixture(session, Path("fixtures/phase1.json"))


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def headers(user: str, admin: bool = False) -> dict[str, str]:
    result = {"X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": user}
    if admin:
        result["X-Rainstone-Admin"] = "true"
    return result
