from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator


class ReportQuery(BaseModel):
    """The single filter contract shared by every reporting endpoint."""

    model_config = ConfigDict(frozen=True)
    basis: Literal["additional", "allocated"] = "additional"
    currency: Literal["USD"] = "USD"
    mode: Literal["accrued", "completed"] = "accrued"
    from_time: datetime | None = None
    to_time: datetime | None = None
    timezone: str = "UTC"
    search: str | None = None
    tool_id: str | None = None
    tool_version: str | None = None
    invocation_id: str | None = None
    workflow_id: str | None = None
    owner: str | None = None
    state: str | None = None
    runner: str | None = None
    destination: str | None = None
    capacity: str | None = None
    quality: str | None = None
    min_cost: Decimal | None = None
    max_cost: Decimal | None = None
    revision: str | None = None
    limit: int = 50
    offset: int = 0
    sort: str = "created_at"
    direction: Literal["asc", "desc"] = "desc"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value


def report_query(
    basis: Literal["additional", "allocated"] = "additional",
    currency: str = "USD",
    mode: Literal["accrued", "completed"] = "accrued",
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    timezone: str = "UTC",
    search: str | None = Query(default=None, max_length=200),
    tool_id: str | None = Query(default=None, max_length=500),
    tool_version: str | None = Query(default=None, max_length=100),
    invocation_id: str | None = None,
    workflow_id: str | None = Query(default=None, max_length=300),
    owner: str | None = Query(default=None, max_length=200),
    state: str | None = Query(default=None, max_length=40),
    runner: str | None = Query(default=None, max_length=100),
    destination: str | None = Query(default=None, max_length=200),
    capacity: str | None = Query(default=None, max_length=40),
    quality: str | None = Query(default=None, max_length=40),
    min_cost: Decimal | None = Query(default=None, ge=0),
    max_cost: Decimal | None = Query(default=None, ge=0),
    revision: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="created_at", max_length=40),
    direction: Literal["asc", "desc"] = "desc",
) -> ReportQuery:
    if currency != "USD":
        raise HTTPException(422, "Only USD reporting is currently supported")
    for label, value in (("from", from_time), ("to", to_time)):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(422, f"{label} must include an explicit UTC offset")
    if from_time and to_time and from_time >= to_time:
        raise HTTPException(422, "The report interval must satisfy from < to")
    try:
        return ReportQuery(**locals() | {"currency": "USD"})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
