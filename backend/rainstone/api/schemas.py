from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class APIModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ObservationWindow(APIModel):
    from_: datetime | None = None
    to: datetime | None = None
    timezone: str
    semantics: str
    mode: str

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Coverage(APIModel):
    jobs: int
    priced: int
    incomplete: int
    known_zero: int
    temporally_unattributed: int


class ReportMeta(APIModel):
    basis: Literal["additional", "allocated"]
    currency: Literal["USD"]
    applied_filters: dict[str, Any]
    observation_window: dict[str, Any]
    revision_id: str | None
    calculation_version: str | None
    as_of: datetime | None
    priced_subtotal: str | None
    coverage: Coverage


class SummaryResponse(ReportMeta):
    amount: str | None
    job_count: int
    priced_job_count: int
    unpriced_job_count: int
    known_zero_job_count: int
    failed_spend: str
    retried_spend: str
    baseline_infrastructure_amount: str | None
    can_view_infrastructure: bool
    demo: bool


class JobItem(APIModel):
    id: str
    source_id: str
    tool_id: str
    tool_version: str | None
    owner: str
    owner_id: str
    state: str
    runner: str | None
    destination: str | None
    created_at: datetime
    updated_at: datetime
    amount: str | None
    currency: Literal["USD"]
    quality: str
    reason: str
    cost_lines: int
    attempt_count: int
    capacities: list[str]
    unattributed_amount: str
    temporally_unattributed: bool
    completed_at: datetime | None


class JobListResponse(APIModel):
    items: list[JobItem]
    total: int
    limit: int
    offset: int
    meta: ReportMeta


class JobDetailResponse(JobItem):
    interval_amount: str | None
    full_job_amount: str | None
    basis: str
    attempts: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    revision_id: str | None
    cost: dict[str, Any]


class ToolStatistics(APIModel):
    cohort: str
    sample_count: int
    excluded_count: int
    mean: str | None
    median: str | None
    p95: str | None
    method: str
    approximate: bool


class ToolItem(APIModel):
    tool_id: str
    tool_version: str | None
    job_count: int
    amount: str | None
    priced_count: int
    incomplete_count: int
    statistics: ToolStatistics


class ToolListResponse(APIModel):
    items: list[ToolItem]
    total: int
    meta: ReportMeta


class InvocationItem(APIModel):
    id: str
    source_id: str
    workflow_id: str
    workflow_name: str
    workflow_version: str | None
    parent_id: str | None
    state: str
    job_count: int
    amount: str | None
    currency: Literal["USD"]
    unpriced_job_count: int
    reused_job_count: int


class InvocationListResponse(APIModel):
    items: list[InvocationItem]
    total: int
    meta: ReportMeta


class InvocationDetailResponse(InvocationItem):
    steps: list[dict[str, Any]]
    children: list[InvocationItem]
    meta: ReportMeta


class DailyItem(APIModel):
    date: str
    amount: str
    currency: Literal["USD"]
    job_count: int
    incomplete_count: int
    provisional: bool
    by_runner: dict[str, str]
    by_owner: dict[str, str]
    by_tool: dict[str, str]


class DailyResponse(APIModel):
    items: list[DailyItem]
    label: str
    temporally_unattributed_count: int
    temporally_unattributed_subtotal: str
    meta: ReportMeta


class UserItem(APIModel):
    owner_id: str
    label: str
    job_count: int
    amount: str | None
    priced_count: int
    incomplete_count: int


class UserListResponse(APIModel):
    items: list[UserItem]
    total: int
    meta: ReportMeta


class InfrastructureResponse(APIModel):
    items: list[dict[str, Any]]
    amount: str | None
    currency: Literal["USD"]
    scope: str
    allocation_supported: bool
    allocation_reason: str
    observation_window: dict[str, Any]
    revision_id: str | None
    as_of: datetime | None


class FreshnessResponse(APIModel):
    sources: list[dict[str, Any]]
    overall_status: str
    observation_gaps: list[dict[str, Any]]


class MeResponse(APIModel):
    source_id: str
    label: str
    is_admin: bool
    auth_mode: str
    attribution: str
    capabilities: dict[str, bool]


class StatusCheck(APIModel):
    name: str
    status: str
    detail: str
    facts: dict[str, Any]


class StatusResponse(APIModel):
    generated_at: str
    overall_status: str
    auth_mode: str
    tenant: str
    checks: list[StatusCheck]
    failed_capabilities: list[str]


class CatalogResponse(APIModel):
    active_catalog_id: str | None
    observed_at: str | None
    imported_at: str | None
    signature_key_id: str | None
    provenance: dict[str, Any]
    supported: list[dict[str, Any]]
