"""Kubernetes execution observation.

Pods are observed through list plus watch, with resource-version tracking,
relisting after an expired version, and explicit gap records when pods may have
been deleted while the collector was disconnected. A pod's occupancy of a node
is one chargeable lifetime; the node name it ran on is not by itself a verified
VM identity, so that qualification travels with the observation.
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from rainstone.adapters.contracts import (
    NormalizedAttempt,
    NormalizedGap,
    NormalizedLifetime,
    NormalizedSegment,
    ObservationBatch,
)
from rainstone.models import CapacityRelationship

SOURCE_NAME = "kubernetes"
JOB_LABEL = "app.galaxyproject.org/job_id"
DESTINATION_LABEL = "app.galaxyproject.org/destination"
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


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


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass(frozen=True)
class NodeDescriptor:
    name: str
    provider_id: str | None = None
    machine_type: str | None = None
    region: str | None = None
    zone: str | None = None
    purchase_model: str | None = None
    capacity: dict = field(default_factory=dict)

    @property
    def resource_uid(self) -> str:
        return self.provider_id or self.name


def node_descriptor(node: dict) -> NodeDescriptor:
    labels = node.get("metadata", {}).get("labels", {})
    spec = node.get("spec", {})
    purchase = "spot" if labels.get("cloud.google.com/gke-spot") == "true" else None
    return NodeDescriptor(
        name=node["metadata"]["name"],
        provider_id=spec.get("providerID"),
        machine_type=labels.get("node.kubernetes.io/instance-type")
        or labels.get("beta.kubernetes.io/instance-type"),
        region=labels.get("topology.kubernetes.io/region"),
        zone=labels.get("topology.kubernetes.io/zone"),
        purchase_model=purchase,
        capacity=node.get("status", {}).get("capacity", {}),
    )


class KubernetesClient(Protocol):
    def list_pods(self, *, continue_token: str | None = None) -> dict: ...

    def watch_pods(self, *, resource_version: str, timeout_seconds: int) -> Iterator[dict]: ...

    def list_nodes(self) -> dict: ...


class ResourceVersionExpired(RuntimeError):
    """The watch cannot resume from the stored resource version."""


class HttpKubernetesClient:
    """In-cluster API access using the pod's own service-account credentials.

    An operator kubeconfig is never read: the deployment authenticates as its
    own workload identity.
    """

    def __init__(
        self,
        *,
        namespace: str,
        api_server: str | None = None,
        token_path: Path = SERVICE_ACCOUNT_DIR / "token",
        ca_path: Path = SERVICE_ACCOUNT_DIR / "ca.crt",
        label_selector: str = JOB_LABEL,
        page_limit: int = 200,
    ) -> None:
        self._namespace = namespace
        self._api_server = api_server or "https://kubernetes.default.svc"
        self._token_path = token_path
        self._context = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else None
        self._label_selector = label_selector
        self._page_limit = page_limit

    def _request(self, path: str, params: dict, *, stream: bool = False):
        url = f"{self._api_server}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token_path.read_text().strip()}"}
        )
        timeout = params.get("timeoutSeconds", 30) + 10 if stream else 30
        return urllib.request.urlopen(request, timeout=timeout, context=self._context)  # noqa: S310

    def list_pods(self, *, continue_token: str | None = None) -> dict:
        params = {"labelSelector": self._label_selector, "limit": self._page_limit}
        if continue_token:
            params["continue"] = continue_token
        with self._request(f"/api/v1/namespaces/{self._namespace}/pods", params) as response:
            return json.load(response)

    def watch_pods(self, *, resource_version: str, timeout_seconds: int) -> Iterator[dict]:
        params = {
            "labelSelector": self._label_selector,
            "watch": "true",
            "resourceVersion": resource_version,
            "timeoutSeconds": timeout_seconds,
            "allowWatchBookmarks": "true",
        }
        try:
            with self._request(
                f"/api/v1/namespaces/{self._namespace}/pods", params, stream=True
            ) as response:
                for line in response:
                    if line.strip():
                        yield json.loads(line)
        except urllib.error.HTTPError as error:
            if error.code == 410:
                raise ResourceVersionExpired(str(error)) from error
            raise

    def list_nodes(self) -> dict:
        with self._request("/api/v1/nodes", {"limit": self._page_limit}) as response:
            return json.load(response)


def _admitted_resources(pod: dict) -> tuple[Decimal | None, Decimal | None]:
    """Sum admitted requests across init and regular containers."""
    cpu = Decimal(0)
    memory = Decimal(0)
    seen_cpu = seen_memory = False
    init_cpu = init_memory = Decimal(0)
    for container in pod["spec"].get("containers", []):
        requests = container.get("resources", {}).get("requests", {})
        if "cpu" in requests:
            cpu += parse_cpu(requests["cpu"])
            seen_cpu = True
        if "memory" in requests:
            memory += parse_memory_mib(requests["memory"])
            seen_memory = True
    for container in pod["spec"].get("initContainers", []):
        requests = container.get("resources", {}).get("requests", {})
        if "cpu" in requests:
            init_cpu = max(init_cpu, parse_cpu(requests["cpu"]))
            seen_cpu = True
        if "memory" in requests:
            init_memory = max(init_memory, parse_memory_mib(requests["memory"]))
            seen_memory = True
    return (
        max(cpu, init_cpu) if seen_cpu else None,
        max(memory, init_memory) if seen_memory else None,
    )


def _condition_time(pod: dict, condition_type: str) -> datetime | None:
    for condition in pod.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type:
            return _time(condition.get("lastTransitionTime"))
    return None


def _container_segments(pod: dict) -> list[NormalizedSegment]:
    """Each container run is its own segment, so restarts stay separate."""
    segments: list[NormalizedSegment] = []
    for status in pod.get("status", {}).get("containerStatuses", []):
        name = status.get("name", "container")
        previous = (status.get("lastState") or {}).get("terminated")
        if previous:
            segments.append(
                NormalizedSegment(
                    source_segment_id=f"{name}:restart",
                    observed_start=_time(previous.get("startedAt")),
                    observed_end=_time(previous.get("finishedAt")),
                    timing_method="kubernetes_container_restart",
                    facts={"exit_code": previous.get("exitCode"), "reason": previous.get("reason")},
                )
            )
        state = status.get("state") or {}
        terminated = state.get("terminated")
        running = state.get("running")
        if terminated:
            segments.append(
                NormalizedSegment(
                    source_segment_id=f"{name}:final",
                    observed_start=_time(terminated.get("startedAt")),
                    observed_end=_time(terminated.get("finishedAt")),
                    timing_method="kubernetes_container_run",
                    facts={"exit_code": terminated.get("exitCode")},
                )
            )
        elif running:
            segments.append(
                NormalizedSegment(
                    source_segment_id=f"{name}:running",
                    observed_start=_time(running.get("startedAt")),
                    observed_end=None,
                    timing_method="kubernetes_container_run",
                    facts={"in_progress": True},
                )
            )
    return segments


def _tool_window(pod: dict) -> tuple[datetime | None, datetime | None]:
    segments = [
        segment
        for segment in _container_segments(pod)
        if segment.timing_method == "kubernetes_container_run"
    ]
    starts = [segment.observed_start for segment in segments if segment.observed_start]
    ends = [segment.observed_end for segment in segments if segment.observed_end]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _exit_code(pod: dict) -> int | None:
    codes = [
        (status.get("state") or {}).get("terminated", {}).get("exitCode")
        for status in pod.get("status", {}).get("containerStatuses", [])
    ]
    present = [code for code in codes if code is not None]
    return max(present) if present else None


def normalize_pod(
    pod: dict,
    *,
    nodes: dict[str, NodeDescriptor],
    baseline_resource_ids: Sequence[str],
    baseline_descriptor: dict | None = None,
) -> NormalizedAttempt | None:
    labels = pod["metadata"].get("labels", {})
    job_source_id = labels.get(JOB_LABEL)
    if not job_source_id:
        return None
    node_name = pod["spec"].get("nodeName")
    node = nodes.get(node_name) if node_name else None
    resource_uid = node.resource_uid if node else (node_name or "unassigned")
    baseline = set(baseline_resource_ids)
    relationship = (
        CapacityRelationship.existing
        if resource_uid in baseline or (node_name and node_name in baseline)
        else CapacityRelationship.unknown
    )
    # This deployment's node exposes no provider ID, instance type or topology
    # labels, so the boot-supplied descriptor names the baseline VM's shape.
    descriptor = baseline_descriptor or {}
    baseline_shape = descriptor if relationship == CapacityRelationship.existing else {}
    reserved_from = _condition_time(pod, "PodScheduled") or _time(pod["status"].get("startTime"))
    segments = _container_segments(pod)
    released_at = max(
        (segment.observed_end for segment in segments if segment.observed_end),
        default=None,
    )
    tool_started, tool_finished = _tool_window(pod)
    vcpu, memory = _admitted_resources(pod)
    owner = next(iter(pod["metadata"].get("ownerReferences", [])), {})
    lifetime = NormalizedLifetime(
        provider="kubernetes",
        # Occupancy of node capacity is keyed by pod UID; the node keeps its own
        # identity in resource_uid so placement stays auditable.
        resource_key=f"pod:{pod['metadata']['uid']}",
        resource_uid=resource_uid,
        capacity_relationship=relationship,
        timing_method="kubernetes_pod_occupancy",
        region=(node.region if node else None) or baseline_shape.get("region"),
        zone=(node.zone if node else None) or baseline_shape.get("zone"),
        machine_type=(node.machine_type if node else None) or baseline_shape.get("machine_type"),
        purchase_model=(node.purchase_model if node else None) or baseline_shape.get("purchase_model"),
        observed_start=reserved_from,
        observed_end=released_at,
        requested_vcpu=vcpu,
        requested_memory_mib=memory,
        segments=tuple(segments),
        facts={
            "pod_uid": pod["metadata"]["uid"],
            "pod_name": pod["metadata"].get("name"),
            "node": node_name,
            "node_provider_id": node.provider_id if node else None,
            "verified_vm_identity": bool(node and node.provider_id),
            "shape_source": "kubernetes_node_labels"
            if (node and node.machine_type)
            else ("baseline_descriptor" if baseline_shape.get("machine_type") else "unknown"),
            "destination": labels.get(DESTINATION_LABEL),
            "owner_kind": owner.get("kind"),
            "owner_uid": owner.get("uid"),
            "queued_until": reserved_from.isoformat() if reserved_from else None,
            "restart_count": sum(
                status.get("restartCount", 0)
                for status in pod.get("status", {}).get("containerStatuses", [])
            ),
        },
    )
    return NormalizedAttempt(
        job_source_id=str(job_source_id),
        source_attempt_id=f"k8s:{pod['metadata']['uid']}",
        runner=SOURCE_NAME,
        outcome=pod.get("status", {}).get("phase", "unknown").lower(),
        external_id=pod["metadata"].get("name"),
        provider_outcome=pod.get("status", {}).get("phase"),
        exit_code=_exit_code(pod),
        tool_started_at=tool_started,
        tool_finished_at=tool_finished,
        lifetimes=(lifetime,),
        correlation="galaxy_pod_label",
        facts={"resource_version": pod["metadata"].get("resourceVersion")},
    )


class KubernetesCollector:
    """One bounded list-or-watch cycle per `collect()` call."""

    source_name = SOURCE_NAME

    def __init__(
        self,
        client: KubernetesClient,
        *,
        baseline_resource_ids: Sequence[str] = (),
        baseline_descriptor: dict | None = None,
        watch_seconds: int = 25,
        now: datetime | None = None,
    ) -> None:
        self._client = client
        self._baseline = tuple(baseline_resource_ids)
        self._baseline_descriptor = baseline_descriptor or {}
        self._watch_seconds = watch_seconds
        self._now = now

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _nodes(self) -> dict[str, NodeDescriptor]:
        listing = self._client.list_nodes()
        return {
            item["metadata"]["name"]: node_descriptor(item) for item in listing.get("items", [])
        }

    def collect(self, cursor: dict) -> ObservationBatch:
        started = self._clock()
        state = dict(cursor)
        nodes = self._nodes()
        resource_version = state.get("resource_version")
        if not resource_version or state.get("continue"):
            return self._relist(state, nodes, started, gaps=())
        try:
            return self._watch(state, nodes, started)
        except ResourceVersionExpired as expired:
            gap = NormalizedGap(
                source=SOURCE_NAME,
                kind="watch_resource_version_expired",
                detail=(
                    "Relisted after an expired watch resource version; pods deleted while "
                    f"disconnected are unrecoverable ({expired})."
                ),
                gap_start=_time(state.get("watched_at")),
                gap_end=started,
                recoverable=False,
            )
            state.pop("resource_version", None)
            return self._relist(state, nodes, started, gaps=(gap,))

    def _relist(
        self,
        state: dict,
        nodes: dict[str, NodeDescriptor],
        started: datetime,
        *,
        gaps: tuple[NormalizedGap, ...],
    ) -> ObservationBatch:
        listing = self._client.list_pods(continue_token=state.get("continue"))
        metadata = listing.get("metadata", {})
        attempts = [
            attempt
            for attempt in (
                normalize_pod(
                    pod,
                    nodes=nodes,
                    baseline_resource_ids=self._baseline,
                    baseline_descriptor=self._baseline_descriptor,
                )
                for pod in listing.get("items", [])
            )
            if attempt is not None
        ]
        continue_token = metadata.get("continue")
        state["continue"] = continue_token or None
        state["relisted_at"] = started.isoformat()
        if not continue_token:
            state["resource_version"] = metadata.get("resourceVersion")
            state["watched_at"] = started.isoformat()
        return ObservationBatch(
            source=SOURCE_NAME,
            observed_at=started,
            attempts=tuple(attempts),
            gaps=gaps,
            cursor=state,
            metrics={"mode": "list", "pods": len(attempts), "nodes": len(nodes)},
            exhausted=not continue_token,
        )

    def _watch(
        self, state: dict, nodes: dict[str, NodeDescriptor], started: datetime
    ) -> ObservationBatch:
        attempts: list[NormalizedAttempt] = []
        resource_version = state["resource_version"]
        deletions = 0
        for event in self._client.watch_pods(
            resource_version=resource_version, timeout_seconds=self._watch_seconds
        ):
            kind = event.get("type")
            item = event.get("object", {})
            if kind == "ERROR":
                raise ResourceVersionExpired(str(item.get("message", "watch error")))
            version = item.get("metadata", {}).get("resourceVersion")
            if version:
                resource_version = version
            if kind == "BOOKMARK":
                continue
            if kind == "DELETED":
                deletions += 1
            attempt = normalize_pod(
                item,
                nodes=nodes,
                baseline_resource_ids=self._baseline,
                baseline_descriptor=self._baseline_descriptor,
            )
            if attempt is not None:
                attempts.append(attempt)
        state["resource_version"] = resource_version
        state["watched_at"] = started.isoformat()
        return ObservationBatch(
            source=SOURCE_NAME,
            observed_at=started,
            attempts=tuple(attempts),
            cursor=state,
            metrics={
                "mode": "watch",
                "events": len(attempts),
                "deletions": deletions,
                "nodes": len(nodes),
            },
            exhausted=True,
        )
