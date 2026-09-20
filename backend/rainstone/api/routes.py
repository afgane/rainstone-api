import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from rainstone.auth import Identity, current_identity
from rainstone.db import get_session
from rainstone.reporting import freshness, job_detail, list_invocations, list_jobs, summary

router = APIRouter(prefix="/api")
Basis = Literal["additional", "allocated"]


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/me")
def me(identity: Identity = Depends(current_identity)) -> dict:
    return {"source_id": identity.source_id, "label": identity.label, "is_admin": identity.is_admin}


@router.get("/summary")
def get_summary(
    basis: Basis = "additional",
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return summary(session, identity, basis)


@router.get("/jobs")
def get_jobs(
    basis: Basis = "additional",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    state: str | None = None,
    runner: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return list_jobs(session, identity, basis, limit, offset, state, runner, search)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID,
    basis: Basis = "additional",
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    result = job_detail(session, identity, job_id, basis)
    if result is None:
        raise HTTPException(404, "Job not found")
    return result


@router.get("/invocations")
def get_invocations(
    basis: Basis = "additional",
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> list[dict]:
    return list_invocations(session, identity, basis)


@router.get("/freshness")
def get_freshness(
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> list[dict]:
    return freshness(session, identity)
