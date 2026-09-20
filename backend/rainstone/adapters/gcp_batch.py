from datetime import datetime
from decimal import Decimal

from rainstone.adapters.contracts import NormalizedAttempt, NormalizedResourceInterval
from rainstone.models import CapacityRelationship


def from_batch_snapshot(batch: dict, lifecycle: dict | None = None) -> NormalizedAttempt:
    allocation = batch["allocationPolicy"]["instances"][0]["policy"]
    resource = batch["taskGroups"][0]["taskSpec"]["computeResource"]
    start = end = None
    method = "batch_task_only"
    if lifecycle:
        start = datetime.fromisoformat(lifecycle["insert_completed"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(lifecycle["delete_requested"].replace("Z", "+00:00"))
        method = "compute_insert_complete_to_delete_request"
    interval = NormalizedResourceInterval(
        source_interval_id=lifecycle["vm_id"] if lifecycle else batch["uid"],
        resource_uid=lifecycle["vm_id"] if lifecycle else batch["uid"],
        provider="gcp",
        region=batch["region"],
        machine_type=allocation["machineType"],
        purchase_model=allocation.get("provisioningModel", "STANDARD").lower(),
        capacity_relationship=CapacityRelationship.dedicated,
        observed_start=start,
        observed_end=end,
        timing_method=method,
        requested_vcpu=Decimal(str(resource.get("cpuMilli", 0))) / 1000,
        requested_memory_mib=Decimal(str(resource.get("memoryMib", 0))),
        facts={"batch_uid": batch["uid"], "full_name": batch["name"], "snapshot": True},
    )
    return NormalizedAttempt(
        str(batch["labels"]["galaxy-job-id"]),
        batch.get("taskAttempt", "task-0-attempt-0"),
        "gcp_batch",
        batch["name"],
        batch["status"]["state"].lower(),
        None,
        None,
        (interval,),
    )
