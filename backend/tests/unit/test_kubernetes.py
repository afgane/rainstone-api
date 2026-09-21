import json
from decimal import Decimal
from pathlib import Path

from rainstone.adapters.kubernetes import (
    KubernetesCollector,
    ResourceVersionExpired,
    node_descriptor,
    normalize_pod,
    parse_cpu,
    parse_memory_mib,
)
from rainstone.models import CapacityRelationship

CONTRACT = Path("fixtures/contract")
BASELINE = ("galaxy-baseline-vm",)


def load(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text())


def nodes() -> dict:
    return {
        item["metadata"]["name"]: node_descriptor(item) for item in load("kubernetes-nodes.json")["items"]
    }


class FakeKubernetesClient:
    def __init__(self, *, events: list | None = None, expire: bool = False, pages: int = 1):
        self._events = events or []
        self._expire = expire
        self._pages = pages
        self.list_calls = 0

    def list_pods(self, *, continue_token: str | None = None) -> dict:
        self.list_calls += 1
        listing = load("kubernetes-pods.json")
        if self._pages > 1 and continue_token is None:
            return {
                "metadata": {"continue": "next-page"},
                "items": listing["items"][:1],
            }
        return listing

    def watch_pods(self, *, resource_version: str, timeout_seconds: int):
        if self._expire:
            raise ResourceVersionExpired("too old resource version: 4300 (9000)")
        yield from self._events

    def list_nodes(self) -> dict:
        return load("kubernetes-nodes.json")


def test_kubernetes_quantities_preserve_units() -> None:
    assert parse_cpu("500m") == Decimal("0.5")
    assert parse_memory_mib("3.8Gi") == Decimal("3891.2")
    assert parse_memory_mib("4080218931200m") == Decimal("3891.2")


def test_baseline_node_placement_is_existing_capacity() -> None:
    pod = load("kubernetes-pods.json")["items"][0]
    attempt = normalize_pod(pod, nodes=nodes(), baseline_resource_ids=BASELINE)
    lifetime = attempt.lifetimes[0]
    assert lifetime.capacity_relationship == CapacityRelationship.existing
    assert lifetime.resource_key == "pod:pod-uid-1498"
    assert lifetime.facts["verified_vm_identity"] is False
    assert attempt.job_source_id == "1498"


def test_unlabelled_baseline_node_takes_its_shape_from_the_descriptor() -> None:
    """The observed deployment's node exposes no instance-type label."""
    pod = load("kubernetes-pods.json")["items"][0]
    attempt = normalize_pod(
        pod,
        nodes=nodes(),
        baseline_resource_ids=BASELINE,
        baseline_descriptor={"machine_type": "t2d-standard-4", "region": "us-central1"},
    )
    lifetime = attempt.lifetimes[0]
    assert lifetime.machine_type == "t2d-standard-4"
    assert lifetime.region == "us-central1"
    assert lifetime.facts["shape_source"] == "baseline_descriptor"


def test_a_descriptor_never_supplies_a_shape_for_unverified_placement() -> None:
    pod = load("kubernetes-pods.json")["items"][2]
    attempt = normalize_pod(
        pod,
        nodes=nodes(),
        baseline_resource_ids=BASELINE,
        baseline_descriptor={"machine_type": "t2d-standard-4", "region": "us-central1"},
    )
    lifetime = attempt.lifetimes[0]
    assert lifetime.capacity_relationship == CapacityRelationship.unknown
    assert lifetime.machine_type == "n2-standard-2"
    assert lifetime.facts["shape_source"] == "kubernetes_node_labels"


def test_unmatched_node_placement_is_unknown_not_zero() -> None:
    pod = load("kubernetes-pods.json")["items"][2]
    attempt = normalize_pod(pod, nodes=nodes(), baseline_resource_ids=BASELINE)
    lifetime = attempt.lifetimes[0]
    assert lifetime.capacity_relationship == CapacityRelationship.unknown
    assert lifetime.machine_type == "n2-standard-2"
    assert lifetime.facts["verified_vm_identity"] is True
    assert lifetime.observed_end is None


def test_container_restart_is_a_separate_segment_and_occupancy_excludes_queueing() -> None:
    pod = load("kubernetes-pods.json")["items"][1]
    attempt = normalize_pod(pod, nodes=nodes(), baseline_resource_ids=BASELINE)
    lifetime = attempt.lifetimes[0]
    assert [segment.source_segment_id for segment in lifetime.segments] == [
        "k8s:restart",
        "k8s:final",
    ]
    assert lifetime.observed_start.isoformat() == "2026-09-21T13:38:12+00:00"
    assert attempt.tool_started_at.isoformat() == "2026-09-21T13:38:35+00:00"
    assert lifetime.facts["restart_count"] == 1


def test_admitted_requests_account_for_init_containers() -> None:
    pod = load("kubernetes-pods.json")["items"][0]
    attempt = normalize_pod(pod, nodes=nodes(), baseline_resource_ids=BASELINE)
    lifetime = attempt.lifetimes[0]
    assert lifetime.requested_vcpu == Decimal("1")
    assert lifetime.requested_memory_mib == Decimal("3891.2")


def test_first_cycle_lists_and_records_the_resource_version() -> None:
    collector = KubernetesCollector(FakeKubernetesClient(), baseline_resource_ids=BASELINE)
    batch = collector.collect({})
    assert len(batch.attempts) == 3
    assert batch.cursor["resource_version"] == "4300"
    assert batch.metrics["mode"] == "list"
    assert batch.exhausted is True


def test_paged_listing_is_not_exhausted_until_the_last_page() -> None:
    collector = KubernetesCollector(
        FakeKubernetesClient(pages=2), baseline_resource_ids=BASELINE
    )
    first = collector.collect({})
    assert first.exhausted is False
    assert first.cursor["continue"] == "next-page"
    assert "resource_version" not in first.cursor
    second = collector.collect(first.cursor)
    assert second.exhausted is True
    assert second.cursor["resource_version"] == "4300"


def test_watch_consumes_events_and_advances_the_resource_version() -> None:
    pods = load("kubernetes-pods.json")["items"]
    events = [
        {"type": "MODIFIED", "object": {**pods[0], "metadata": {**pods[0]["metadata"], "resourceVersion": "4400"}}},
        {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "4500"}}},
    ]
    collector = KubernetesCollector(
        FakeKubernetesClient(events=events), baseline_resource_ids=BASELINE
    )
    batch = collector.collect({"resource_version": "4300"})
    assert len(batch.attempts) == 1
    assert batch.cursor["resource_version"] == "4500"
    assert batch.metrics["mode"] == "watch"


def test_expired_watch_relists_and_records_an_unrecoverable_gap() -> None:
    client = FakeKubernetesClient(expire=True)
    collector = KubernetesCollector(client, baseline_resource_ids=BASELINE)
    batch = collector.collect({"resource_version": "4300", "watched_at": "2026-09-21T13:00:00Z"})
    assert client.list_calls == 1
    assert batch.gaps[0].kind == "watch_resource_version_expired"
    assert batch.gaps[0].recoverable is False
    assert batch.cursor["resource_version"] == "4300"
