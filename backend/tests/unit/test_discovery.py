"""Discovery reads existing configuration and emits a redacted description."""

from rainstone.discovery import Discovery, HostMetadata, discover

NAMESPACE = "galaxy"
RELEASE = "galaxy"

GALAXY_YAML = """
galaxy:
  single_user: researcher@example.org
  database_connection: postgresql://galaxydbuser:hunter2@galaxy-postgres-rw/galaxy
"""

JOB_CONF = """
runners:
  local:
    load: galaxy.jobs.runners.local:LocalJobRunner
  k8s:
    load: galaxy.jobs.runners.kubernetes:KubernetesJobRunner
  batch:
    load: galaxy.jobs.runners.gcp_batch:GCPBatchJobRunner
    project_id: anvil-and-terra-development
    region: us-east4
execution:
  default: k8s
  environments:
    local:
      runner: local
    k8s:
      runner: k8s
      k8s_namespace: galaxy
    gcp_batch:
      runner: batch
"""

CLUSTER = {
    "items": [
        {
            "metadata": {"name": "galaxy-postgres", "labels": {}},
            "spec": {
                "storage": {"storageClass": "postgres-storage"},
                "bootstrap": {
                    "initdb": {
                        "database": "galaxy",
                        "owner": "galaxydbuser",
                        "secret": {
                            "name": "galaxydbuser.galaxy-postgres.credentials.postgresql.cnpg.io"
                        },
                    }
                }
            },
        }
    ]
}

OBJECTS = {
    "/apis/apps/v1/deployments?labelSelector=app.kubernetes.io%2Fname%3Dgalaxy": {
        "items": [
            {
                "metadata": {
                    "namespace": NAMESPACE,
                    "labels": {"app.kubernetes.io/instance": RELEASE},
                }
            }
        ]
    },
    f"/apis/postgresql.cnpg.io/v1/namespaces/{NAMESPACE}/clusters": CLUSTER,
    f"/api/v1/namespaces/{NAMESPACE}/configmaps/{RELEASE}-configs": {
        "data": {"galaxy.yml": GALAXY_YAML, "job_conf.yml": JOB_CONF}
    },
    f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}/ingresses": {
        "items": [
            {
                "spec": {
                    "rules": [
                        {
                            "http": {
                                "paths": [
                                    {
                                        "path": "/proxy/google/v1/apps/project/app/galaxy",
                                        "backend": {"service": {"name": f"{RELEASE}-nginx"}},
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        ]
    },
    f"/api/v1/namespaces/{NAMESPACE}/persistentvolumeclaims": {
        "items": [
            {
                "metadata": {"name": f"{RELEASE}-postgres-1"},
                "spec": {"storageClassName": "postgres-storage"},
            }
        ]
    },
    "/apis/storage.k8s.io/v1/storageclasses/postgres-storage": {"reclaimPolicy": "Retain"},
}

HOST = {
    "project/project-id": "anvil-and-terra-development",
    "instance/id": "8649021727128195799",
    "instance/name": "galaxy-terra-app-3bfddf0c",
    "instance/machine-type": "projects/526897014808/machineTypes/t2d-standard-4",
    "instance/zone": "projects/526897014808/zones/us-central1-a",
}


class FakeApi:
    def __init__(self, objects: dict) -> None:
        self.objects = objects

    def get(self, path: str) -> dict:
        return self.objects[path]

    def try_get(self, path: str) -> dict | None:
        return self.objects.get(path)


class FakeMetadata(HostMetadata):
    def __init__(self, values: dict | None) -> None:
        self.values = values

    def _read(self, path: str) -> str | None:
        return (self.values or {}).get(path)


def run(objects: dict | None = None, host: dict | None = HOST, **kwargs) -> Discovery:
    return discover(
        api=FakeApi(OBJECTS if objects is None else objects),
        metadata=FakeMetadata(host),
        **kwargs,
    )


def test_discovery_resolves_the_deployment_from_existing_configuration() -> None:
    values = run().values()

    assert values["instance"]["slug"] == "galaxy-galaxy"
    assert values["auth"]["workspaceOwner"] == "researcher@example.org"
    assert values["basePath"] == "/proxy/google/v1/apps/project/app/galaxy/costs"
    assert values["source"] == {
        "existingSecret": "galaxydbuser.galaxy-postgres.credentials.postgresql.cnpg.io",
        "usernameKey": "username",
        "passwordKey": "password",
        "host": "galaxy-postgres-rw",
        "port": 5432,
        "database": "galaxy",
    }
    assert values["collector"]["kubernetes"] == {"enabled": True, "namespace": "galaxy"}
    assert values["database"]["storageClass"] == "postgres-storage"


def test_discovery_emits_secret_references_and_never_a_credential() -> None:
    description = run()
    rendered = repr(description.values()) + repr(description.report())

    assert "hunter2" not in rendered
    assert "database_connection" not in rendered
    # The superuser Secret is never the one referenced.
    assert "postgres.galaxy-postgres.credentials" not in rendered


def test_batch_is_read_from_the_runner_not_from_the_host_vm() -> None:
    values = run().values()

    # Batch runs in its own project and region, which need not be the host's.
    assert values["collector"]["gcpBatch"] == {
        "enabled": True,
        "project": "anvil-and-terra-development",
        "location": "us-east4",
    }
    assert values["baseline"]["region"] == "us-central1"


def test_the_baseline_names_only_work_that_runs_in_the_galaxy_server() -> None:
    """A Batch VM is separately billed, and a pod's node is observed, not assumed."""
    baseline = run().values()["baseline"]
    assert baseline["runners"] == "local"
    # The destinations using that runner are named, so work Galaxy records under
    # a destination is matched explicitly rather than through its runner.
    assert baseline["destinations"] == "local"


def test_dynamically_assigned_destinations_are_a_reason_not_a_guess() -> None:
    objects = dict(OBJECTS)
    objects[f"/api/v1/namespaces/{NAMESPACE}/configmaps/{RELEASE}-configs"] = {
        "data": {
            "galaxy.yml": GALAXY_YAML,
            "job_conf.yml": JOB_CONF.split("execution:")[0]
            + "execution:\n  default: tpv\n  environments:\n    tpv:\n      runner: dynamic\n",
        }
    }
    description = run(objects)

    assert description.values()["baseline"]["runners"] == "local"
    assert "destinations" not in description.values()["baseline"]
    assert "assigned dynamically" in description.report()["unresolved"]["baseline_destinations"]


def test_the_host_descriptor_comes_from_vm_metadata_without_kubernetes_labels() -> None:
    values = run().values()

    assert values["baseline"]["resourceUid"] == "8649021727128195799"
    assert values["baseline"]["machineType"] == "t2d-standard-4"
    assert values["baseline"]["zone"] == "us-central1-a"
    assert values["baseline"]["policyVersion"] == "host-8649021727128195799"
    # A node without a provider ID reports only its name, so the observation is
    # joined on that as well as on the provider's numeric ID.
    assert values["baseline"]["nodeNames"] == "galaxy-terra-app-3bfddf0c"


def test_an_unreachable_metadata_server_leaves_the_baseline_visibly_unresolved() -> None:
    description = run(host=None)

    assert "host_machine_type" in description.report()["unresolved"]
    assert "baseline" not in description.values()
    # Everything that did resolve is still usable.
    assert description.values()["source"]["host"] == "galaxy-postgres-rw"


def test_several_galaxy_releases_are_reported_rather_than_guessed() -> None:
    objects = dict(OBJECTS)
    objects["/apis/apps/v1/deployments?labelSelector=app.kubernetes.io%2Fname%3Dgalaxy"] = {
        "items": [
            {"metadata": {"namespace": "a", "labels": {"app.kubernetes.io/instance": "galaxy"}}},
            {"metadata": {"namespace": "b", "labels": {"app.kubernetes.io/instance": "galaxy"}}},
        ]
    }
    description = run(objects)

    assert "several Galaxy releases" in description.report()["unresolved"]["namespace"]
    assert description.values() == {}


def test_a_missing_shared_account_is_a_reason_not_a_guess() -> None:
    objects = dict(OBJECTS)
    objects[f"/api/v1/namespaces/{NAMESPACE}/configmaps/{RELEASE}-configs"] = {
        "data": {"galaxy.yml": "galaxy: {}", "job_conf.yml": JOB_CONF}
    }
    description = run(objects)

    assert "names no single or shared account" in description.report()["unresolved"]["shared_account"]
    assert "workspaceOwner" not in description.values()["auth"]


def test_a_non_retaining_storage_class_is_called_out() -> None:
    objects = dict(OBJECTS)
    objects["/apis/storage.k8s.io/v1/storageclasses/postgres-storage"] = {
        "reclaimPolicy": "Delete"
    }
    description = run(objects)

    assert any("survives stop and resume" in note for note in description.report()["notes"])
