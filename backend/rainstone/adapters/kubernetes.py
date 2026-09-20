from datetime import datetime
from decimal import Decimal

from rainstone.adapters.contracts import NormalizedAttempt, NormalizedResourceInterval
from rainstone.models import CapacityRelationship


def parse_cpu(value: str) -> Decimal:
    return Decimal(value[:-1]) / Decimal("1000") if value.endswith("m") else Decimal(value)


def parse_memory_mib(value: str) -> Decimal:
    suffixes = {"Ki": Decimal(1) / 1024, "Mi": Decimal(1), "Gi": Decimal(1024)}
    for suffix, factor in suffixes.items():
        if value.endswith(suffix):
            return Decimal(value[: -len(suffix)]) * factor
    if value.endswith("m"):
        return Decimal(value[:-1]) / Decimal(1000) / Decimal(1024 * 1024)
    return Decimal(value) / Decimal(1024 * 1024)


def from_pod_snapshot(pod: dict, baseline_resource_ids: set[str]) -> NormalizedAttempt:
    labels = pod["metadata"].get("labels", {})
    job_source_id = labels["app.galaxyproject.org/job_id"]
    node = pod["spec"].get("nodeName")
    provider_id = pod.get("node", {}).get("provider_id") or node or "unassigned"
    requests = pod["spec"]["containers"][0].get("resources", {}).get("requests", {})
    status = pod["status"]
    started = (
        datetime.fromisoformat(status["startTime"].replace("Z", "+00:00"))
        if status.get("startTime")
        else None
    )
    finished_raw = status.get("containerFinishedAt")
    finished = datetime.fromisoformat(finished_raw.replace("Z", "+00:00")) if finished_raw else None
    relationship = (
        CapacityRelationship.existing
        if provider_id in baseline_resource_ids
        else CapacityRelationship.unknown
    )
    interval = NormalizedResourceInterval(
        source_interval_id=pod["metadata"]["uid"],
        resource_uid=provider_id,
        provider="gcp",
        region=pod.get("node", {}).get("region"),
        machine_type=pod.get("node", {}).get("machine_type"),
        purchase_model=pod.get("node", {}).get("purchase_model"),
        capacity_relationship=relationship,
        observed_start=started,
        observed_end=finished,
        timing_method="kubernetes_pod_occupancy",
        requested_vcpu=parse_cpu(requests["cpu"]) if requests.get("cpu") else None,
        requested_memory_mib=parse_memory_mib(requests["memory"]) if requests.get("memory") else None,
        facts={"pod_uid": pod["metadata"]["uid"], "node": node, "snapshot": True},
    )
    return NormalizedAttempt(
        job_source_id,
        pod["metadata"]["uid"],
        "kubernetes",
        pod["metadata"]["name"],
        status.get("phase", "unknown").lower(),
        started,
        finished,
        (interval,),
    )
