import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from rainstone.api.schemas import (
    DailyResponse,
    FreshnessResponse,
    InfrastructureResponse,
    InvocationDetailResponse,
    InvocationListResponse,
    JobDetailResponse,
    JobListResponse,
    MeResponse,
    SummaryResponse,
    ToolListResponse,
    UserListResponse,
)
from rainstone.auth import Identity, current_identity
from rainstone.db import get_session
from rainstone.report_query import ReportQuery, report_query
from rainstone.reporting import (
    daily,
    export_csv,
    freshness,
    infrastructure,
    invocation_detail,
    invocations,
    job_detail,
    list_jobs,
    summary,
    tools,
    users,
    validate_snapshot,
)

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
def me(identity: Identity = Depends(current_identity)) -> dict:
    return {
        "source_id": identity.source_id, "label": identity.label,
        "is_admin": identity.is_admin,
        "capabilities": {"infrastructure": identity.is_admin, "users": identity.is_admin},
    }


@router.get("/summary", response_model=SummaryResponse)
def get_summary(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return summary(session, identity, query)


@router.get("/jobs", response_model=JobListResponse)
def get_jobs(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return list_jobs(session, identity, query)


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job(
    job_id: uuid.UUID,
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    result = job_detail(session, identity, job_id, query)
    if result is None:
        raise HTTPException(404, "Job not found")
    return result


@router.get("/tools", response_model=ToolListResponse)
def get_tools(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return tools(session, identity, query)


@router.get("/invocations", response_model=InvocationListResponse)
def get_invocations(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return invocations(session, identity, query)


@router.get("/invocations/{invocation_id}", response_model=InvocationDetailResponse)
def get_invocation(
    invocation_id: uuid.UUID,
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    result = invocation_detail(session, identity, invocation_id, query)
    if result is None:
        raise HTTPException(404, "Invocation not found")
    return result


@router.get("/daily", response_model=DailyResponse)
def get_daily(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return daily(session, identity, query)


@router.get("/users", response_model=UserListResponse)
def get_users(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return users(session, identity, query)


@router.get("/infrastructure", response_model=InfrastructureResponse)
def get_infrastructure(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return infrastructure(session, identity, query)


@router.get("/export/jobs.csv")
def get_export(
    query: ReportQuery = Depends(report_query),
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> StreamingResponse:
    validate_snapshot(session, identity, query)
    return StreamingResponse(
        export_csv(session, identity, query),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=rainstone-jobs.csv"},
    )


@router.get("/freshness", response_model=FreshnessResponse)
def get_freshness(
    session: Session = Depends(get_session),
    identity: Identity = Depends(current_identity),
) -> dict:
    return freshness(session, identity)
