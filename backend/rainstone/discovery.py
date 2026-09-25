"""Deployment discovery, shared by manual installation and boot.

Discovery reads what the cluster and the host already describe and emits one
small resolved description. It mutates nothing — not Galaxy, not its database,
not its configuration — and it emits references to existing Secrets rather than
their contents, so no credential passes through the output, a log or a rendered
value.

Manual installation consumes this output as Helm values. Later, k8s-boot can
supply the same description directly, so there is one deployment description
rather than two implementations.

Every field resolves or says why it did not. An unresolved field is visible in
the report and absent from the values, so nothing silently defaults to a
temporary location, another project's prices or a guessed account.
"""

import json
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
METADATA_ROOT = "http://metadata.google.internal/computeMetadata/v1"
GALAXY_NAME_LABEL = "app.kubernetes.io/name=galaxy"
# Only these Galaxy configuration keys are read. Job command lines, destination
# parameters and arbitrary configuration are never parsed or emitted.
GALAXY_CONFIG_KEYS = ("single_user", "admin_users")


class DiscoveryError(RuntimeError):
    """Discovery could not reach the cluster or the host at all."""


@dataclass
class Resolved:
    """One discovered field, or the reason it stayed unresolved."""

    value: object = None
    source: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value not in (None, "", [], {})


def resolved(value: object, source: str) -> Resolved:
    return Resolved(value=value, source=source)


def unresolved(reason: str) -> Resolved:
    return Resolved(reason=reason)


class KubernetesApi:
    """Read-only Kubernetes access, in-cluster or through a local kubectl.

    In-cluster is the production path and needs no kubeconfig. Outside the
    cluster the same paths go through `kubectl get --raw`, which keeps one set
    of request paths instead of a second client and its own credential
    handling.
    """

    def __init__(
        self,
        *,
        service_account_dir: Path = SERVICE_ACCOUNT_DIR,
        api_server: str = "https://kubernetes.default.svc",
        kubectl: str = "kubectl",
    ) -> None:
        self._dir = service_account_dir
        self._api_server = api_server
        self._kubectl = kubectl
        self._in_cluster = (service_account_dir / "token").exists()

    @property
    def in_cluster(self) -> bool:
        return self._in_cluster

    def get(self, path: str) -> dict:
        if self._in_cluster:
            return self._request(path)
        return self._kubectl_raw(path)

    def try_get(self, path: str) -> dict | None:
        try:
            return self.get(path)
        except DiscoveryError:
            return None

    def _request(self, path: str) -> dict:
        token = (self._dir / "token").read_text().strip()
        ca_path = self._dir / "ca.crt"
        context = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else None
        request = urllib.request.Request(
            f"{self._api_server}{path}", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise DiscoveryError(f"{path} returned HTTP {error.code}") from error
        except OSError as error:
            raise DiscoveryError(f"{path} was unreachable: {type(error).__name__}") from error

    def _kubectl_raw(self, path: str) -> dict:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self._kubectl, "get", "--raw", path],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DiscoveryError(f"kubectl could not be run: {type(error).__name__}") from error
        if completed.returncode != 0:
            raise DiscoveryError(f"kubectl get --raw {path} failed: {completed.stderr.strip()[:200]}")
        return json.loads(completed.stdout)

    def namespace(self) -> str | None:
        path = self._dir / "namespace"
        return path.read_text().strip() if path.exists() else None


class HostMetadata:
    """The Galaxy host's own identity, from the instance metadata server.

    This describes the VM Galaxy runs on. It never describes a remote Batch
    worker, whose project and region come from the runner configuration.
    """

    def __init__(self, *, root: str = METADATA_ROOT, timeout: int = 5) -> None:
        self._root = root
        self._timeout = timeout

    def _read(self, path: str) -> str | None:
        request = urllib.request.Request(
            f"{self._root}/{path}", headers={"Metadata-Flavor": "Google"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return response.read().decode().strip()
        except (urllib.error.URLError, OSError):
            return None

    def descriptor(self) -> dict[str, Resolved]:
        project = self._read("project/project-id")
        if project is None:
            reason = "the instance metadata server did not answer; supply a descriptor override"
            fields = ("project", "instance_id", "node_name", "machine_type", "zone", "region")
            return {name: unresolved(reason) for name in fields}
        instance_id = self._read("instance/id")
        name = self._read("instance/name")
        machine_type = (self._read("instance/machine-type") or "").rsplit("/", 1)[-1] or None
        zone = (self._read("instance/zone") or "").rsplit("/", 1)[-1] or None
        region = zone.rsplit("-", 1)[0] if zone else None
        return {
            "project": resolved(project, "instance metadata"),
            "node_name": resolved(name, "instance metadata")
            if name
            else unresolved("the metadata server did not report an instance name"),
            "instance_id": resolved(instance_id, "instance metadata")
            if instance_id
            else unresolved("the metadata server did not report an instance ID"),
            "machine_type": resolved(machine_type, "instance metadata")
            if machine_type
            else unresolved("the metadata server did not report a machine type"),
            "zone": resolved(zone, "instance metadata")
            if zone
            else unresolved("the metadata server did not report a zone"),
            "region": resolved(region, "instance metadata")
            if region
            else unresolved("no zone was reported, so no region could be derived"),
        }


@dataclass
class Discovery:
    """The resolved deployment description, redacted by construction."""

    fields: dict[str, Resolved] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def value(self, name: str, default: object = None) -> object:
        entry = self.fields.get(name)
        return entry.value if entry is not None and entry.ok else default

    def report(self) -> dict:
        return {
            "resolved": {
                name: {"value": entry.value, "source": entry.source}
                for name, entry in self.fields.items()
                if entry.ok
            },
            "unresolved": {
                name: entry.reason for name, entry in self.fields.items() if not entry.ok
            },
            "notes": self.notes,
        }

    def values(self) -> dict:
        """Helm values for the resolved fields only.

        An unresolved field is left out rather than defaulted, so `helm install`
        fails on a missing prerequisite instead of installing a deployment that
        reports against a guess. A description that resolved no Galaxy release
        yields nothing at all.
        """
        if not (self.value("namespace") and self.value("galaxy_release")):
            return {}
        values: dict = {
            "instance": {"slug": self.value("instance_slug")},
            "auth": {"mode": "anvil-workspace", "workspaceOwner": self.value("shared_account")},
            "basePath": self.value("base_path"),
            "source": {
                "existingSecret": self.value("source_secret"),
                "usernameKey": self.value("source_secret_username_key"),
                "passwordKey": self.value("source_secret_password_key"),
                "host": self.value("source_host"),
                "port": self.value("source_port"),
                "database": self.value("source_database"),
                "sslMode": self.value("source_sslmode"),
            },
            "collector": {
                "kubernetes": {"enabled": True, "namespace": self.value("namespace")},
                "gcpBatch": {
                    "project": self.value("batch_project"),
                    "location": self.value("batch_location"),
                },
            },
            "database": {"storageClass": self.value("storage_class")},
        }
        if self.value("baseline_policy_version"):
            values["baseline"] = {
                "policyVersion": self.value("baseline_policy_version"),
                "resourceUid": self.value("host_instance_id"),
                "nodeNames": self.value("host_node_name", ""),
                "machineType": self.value("host_machine_type"),
                "zone": self.value("host_zone"),
                "region": self.value("host_region"),
                "destinations": self.value("baseline_destinations", ""),
                "runners": self.value("baseline_runners", ""),
            }
        if self.value("batch_project") and self.value("batch_location"):
            values["collector"]["gcpBatch"]["enabled"] = True
        return _prune(values)


def _prune(value: object) -> object:
    if isinstance(value, dict):
        pruned = {key: _prune(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if item not in (None, {}, "")}
    return value


def _galaxy_release(api: KubernetesApi, namespace: str | None) -> tuple[Resolved, Resolved]:
    scope = f"/apis/apps/v1/namespaces/{namespace}/deployments" if namespace else "/apis/apps/v1/deployments"
    query = urllib.parse.urlencode({"labelSelector": GALAXY_NAME_LABEL})
    payload = api.try_get(f"{scope}?{query}")
    if payload is None:
        return (
            unresolved("the Kubernetes API could not be read; run this on the Galaxy VM"),
            unresolved("no Galaxy release could be identified"),
        )
    candidates = {
        (item["metadata"]["namespace"], item["metadata"]["labels"].get("app.kubernetes.io/instance"))
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/instance")
    }
    if not candidates:
        return (
            unresolved("no Galaxy deployment carries the app.kubernetes.io/name=galaxy label"),
            unresolved("no Galaxy release could be identified"),
        )
    if len(candidates) > 1:
        listing = ", ".join(sorted(f"{ns}/{release}" for ns, release in candidates))
        reason = f"several Galaxy releases are installed ({listing}); name one explicitly"
        return unresolved(reason), unresolved(reason)
    found_namespace, release = next(iter(candidates))
    return (
        resolved(found_namespace, "Galaxy deployment labels"),
        resolved(release, "Galaxy deployment labels"),
    )


def _source_connection(api: KubernetesApi, namespace: str, release: str) -> dict[str, Resolved]:
    """Describe Galaxy's database from its own PostgreSQL resource.

    The CNPG Cluster names the application owner, its Secret and the database,
    so neither the Secret's name nor the superuser credential is assumed. The
    superuser Secret is deliberately not used: ordinary collection does not need
    administrator privileges.
    """
    clusters = api.try_get(f"/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters")
    items = (clusters or {}).get("items", [])
    owned = [
        item
        for item in items
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/instance", release)
        == release
    ]
    if len(owned) != 1:
        reason = (
            "no CloudNativePG cluster was found for this release; supply the source host, "
            "database and Secret reference explicitly"
            if not owned
            else "several PostgreSQL clusters match this release; name the source explicitly"
        )
        return {
            name: unresolved(reason)
            for name in (
                "source_host",
                "source_database",
                "source_secret",
                "source_secret_username_key",
                "source_secret_password_key",
            )
        }
    cluster = owned[0]
    name = cluster["metadata"]["name"]
    initdb = cluster.get("spec", {}).get("bootstrap", {}).get("initdb", {})
    secret = initdb.get("secret", {}).get("name")
    database = initdb.get("database")
    source = f"CloudNativePG cluster {name}"
    fields = {
        # The primary service, because a single-instance deployment leaves the
        # replica-only `-ro` service without endpoints.
        "source_host": resolved(f"{name}-rw", source),
        "source_database": resolved(database, source)
        if database
        else unresolved("the PostgreSQL cluster does not name its application database"),
        "source_secret": resolved(secret, source)
        if secret
        else unresolved("the PostgreSQL cluster does not name an application credential Secret"),
        "source_secret_username_key": resolved("username", source),
        "source_secret_password_key": resolved("password", source),
    }
    return fields


def _shared_account(api: KubernetesApi, namespace: str, release: str) -> Resolved:
    """Read the configured single or shared account from Galaxy's own config."""
    maps = api.try_get(f"/api/v1/namespaces/{namespace}/configmaps/{release}-configs")
    if maps is None:
        return unresolved(
            "Galaxy's configuration ConfigMap could not be read; name the shared account "
            "explicitly as a username, numeric ID or exact email"
        )
    document = (maps.get("data") or {}).get("galaxy.yml")
    if not document:
        return unresolved("Galaxy's configuration holds no galaxy.yml; name the account explicitly")
    try:
        parsed = yaml.safe_load(document) or {}
    except yaml.YAMLError:
        return unresolved("Galaxy's configuration could not be parsed; name the account explicitly")
    galaxy = parsed.get("galaxy") or {}
    for key in GALAXY_CONFIG_KEYS:
        value = galaxy.get(key)
        if isinstance(value, str) and value.strip():
            first = value.split(",")[0].strip()
            if first:
                return resolved(first, f"Galaxy configuration `{key}`")
        if isinstance(value, list) and value:
            return resolved(str(value[0]).strip(), f"Galaxy configuration `{key}`")
    return unresolved(
        "Galaxy's configuration names no single or shared account; name it explicitly as a "
        "username, numeric ID or exact email"
    )


def _base_path(api: KubernetesApi, namespace: str, release: str) -> Resolved:
    """Register Rainstone under the existing Galaxy prefix, never a new entry point."""
    ingresses = api.try_get(f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses")
    if ingresses is None:
        return unresolved("the cluster's ingresses could not be read; supply the public path")
    paths: list[str] = []
    for item in ingresses.get("items", []):
        for rule in item.get("spec", {}).get("rules", []):
            for entry in rule.get("http", {}).get("paths", []):
                service = entry.get("backend", {}).get("service", {}).get("name", "")
                if service in {f"{release}-nginx", f"{release}-galaxy", release}:
                    paths.append(entry.get("path", "/"))
    if not paths:
        return unresolved("no ingress path routes to Galaxy; supply the public path explicitly")
    prefix = max(paths, key=len).rstrip("/")
    return resolved(f"{prefix}/costs", "Galaxy ingress path")


def _batch_runner(api: KubernetesApi, namespace: str, release: str) -> dict[str, Resolved]:
    """Detect a Batch destination from Galaxy's job configuration.

    Absence of recent Batch jobs is not absence of the runner, and the host VM's
    own project and region are not evidence about where Batch runs, so they are
    only a labeled fallback.
    """
    maps = api.try_get(f"/api/v1/namespaces/{namespace}/configmaps/{release}-configs")
    document = ((maps or {}).get("data") or {}).get("job_conf.yml")
    if not document:
        return {
            "batch_detected": unresolved(
                "Galaxy's job configuration could not be read; enable Batch explicitly if this "
                "deployment uses it"
            ),
            "batch_project": unresolved("no Batch runner configuration was read"),
            "batch_location": unresolved("no Batch runner configuration was read"),
        }
    try:
        parsed = yaml.safe_load(document) or {}
    except yaml.YAMLError:
        return {
            "batch_detected": unresolved("Galaxy's job configuration could not be parsed"),
            "batch_project": unresolved("Galaxy's job configuration could not be parsed"),
            "batch_location": unresolved("Galaxy's job configuration could not be parsed"),
        }
    runners = parsed.get("runners") or {}
    batch = next(
        (
            config
            for config in runners.values()
            if isinstance(config, dict) and "gcp_batch" in str(config.get("load", ""))
        ),
        None,
    )
    if batch is None:
        return {
            "batch_detected": resolved(False, "Galaxy job configuration"),
            "batch_project": unresolved("this Galaxy has no Batch runner configured"),
            "batch_location": unresolved("this Galaxy has no Batch runner configured"),
        }
    # Galaxy's runner names these `project_id` and `region`; accept the plain
    # spellings too rather than depending on one of them.
    project = batch.get("project_id") or batch.get("project")
    location = batch.get("location") or batch.get("region")
    return {
        "batch_detected": resolved(True, "Galaxy job configuration"),
        "batch_project": resolved(project, "Galaxy Batch runner")
        if project
        else unresolved("the Batch runner names no project; Batch need not run in the host's"),
        "batch_location": resolved(location, "Galaxy Batch runner")
        if location
        else unresolved("the Batch runner names no location; Batch need not run in the host's"),
    }


# Only Galaxy's local runner executes inside the Galaxy server process's own
# machine. Every other runner places work somewhere else: Batch and Pulsar
# provision their own capacity, and Kubernetes schedules pods whose placement
# on the server is established per pod by Kubernetes observation, never by the
# runner's name.
HOST_RUNNER_LOAD = "galaxy.jobs.runners.local:"


def _baseline_execution(api: KubernetesApi, namespace: str, release: str) -> dict[str, Resolved]:
    """Name the runners and destinations that execute on the Galaxy server.

    A destination's ID and runner are read; its parameters are not.
    """
    maps = api.try_get(f"/api/v1/namespaces/{namespace}/configmaps/{release}-configs")
    document = ((maps or {}).get("data") or {}).get("job_conf.yml")
    try:
        parsed = yaml.safe_load(document) if document else None
    except yaml.YAMLError:
        parsed = None
    configured = (parsed or {}).get("runners") or {}
    local = sorted(
        name
        for name, config in configured.items()
        if HOST_RUNNER_LOAD in str((config or {}).get("load", ""))
    )
    if not local:
        reason = (
            "Galaxy's configured runners could not be read, or none of them is the local runner; "
            "name the baseline runners explicitly"
        )
        return {"baseline_runners": unresolved(reason), "baseline_destinations": unresolved(reason)}
    environments = ((parsed or {}).get("execution") or {}).get("environments") or {}
    destinations = sorted(
        name
        for name, config in environments.items()
        if isinstance(config, dict) and config.get("runner") in local
    )
    return {
        "baseline_runners": resolved(",".join(local), "Galaxy job configuration"),
        "baseline_destinations": resolved(",".join(destinations), "Galaxy job configuration")
        if destinations
        else unresolved(
            "no statically configured destination uses the local runner; if destinations are "
            "assigned dynamically, name the ones that run on this server explicitly"
        ),
    }


def _storage(api: KubernetesApi, namespace: str, release: str) -> tuple[Resolved, list[str]]:
    """Follow the storage Galaxy's own database persists on.

    Galaxy's claims span several storage classes — application data, reference
    data, a message broker — and only the one its PostgreSQL uses is a
    deliberate choice about durable database storage. Rainstone takes the same
    class for its own volume; it never shares Galaxy's data directory.
    """
    notes: list[str] = []
    clusters = api.try_get(f"/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters")
    classes = {
        item.get("spec", {}).get("storage", {}).get("storageClass")
        for item in (clusters or {}).get("items", [])
        if item.get("spec", {}).get("storage", {}).get("storageClass")
    }
    if len(classes) != 1:
        claims = api.try_get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")
        classes = {
            item["spec"].get("storageClassName")
            for item in (claims or {}).get("items", [])
            if item["metadata"]["name"].startswith(release) and item["spec"].get("storageClassName")
        }
    if len(classes) != 1:
        return (
            unresolved(
                "Galaxy's database storage class could not be identified; name the persistent "
                "storage class for Rainstone's own database explicitly"
            ),
            notes,
        )
    storage_class = next(iter(classes))
    described = api.try_get(f"/apis/storage.k8s.io/v1/storageclasses/{storage_class}")
    policy = (described or {}).get("reclaimPolicy")
    if policy and policy != "Retain":
        notes.append(
            f"storage class '{storage_class}' reclaims volumes with policy '{policy}'; confirm it "
            "survives stop and resume before relying on it for reporting history"
        )
    return resolved(storage_class, "Galaxy's PostgreSQL storage"), notes


def discover(
    *,
    api: KubernetesApi | None = None,
    metadata: HostMetadata | None = None,
    namespace: str | None = None,
    release: str | None = None,
    instance_slug: str | None = None,
    shared_account: str | None = None,
    base_path: str | None = None,
) -> Discovery:
    """Resolve the deployment description, asking only where evidence is absent."""
    api = api or KubernetesApi()
    metadata = metadata or HostMetadata()
    fields: dict[str, Resolved] = {}
    notes: list[str] = []

    found_namespace, found_release = _galaxy_release(api, namespace)
    if namespace:
        found_namespace = resolved(namespace, "explicit override")
    if release:
        found_release = resolved(release, "explicit override")
    fields["namespace"] = found_namespace
    fields["galaxy_release"] = found_release

    if not (found_namespace.ok and found_release.ok):
        return Discovery(fields=fields, notes=notes)

    namespace_name = str(found_namespace.value)
    release_name = str(found_release.value)

    fields.update(_source_connection(api, namespace_name, release_name))
    fields["source_port"] = resolved(5432, "PostgreSQL default")
    fields["source_sslmode"] = unresolved(
        "no TLS mode is configured for the source connection; the cluster default applies"
    )

    fields["shared_account"] = (
        resolved(shared_account, "explicit override")
        if shared_account
        else _shared_account(api, namespace_name, release_name)
    )
    fields["base_path"] = (
        resolved(base_path, "explicit override")
        if base_path
        else _base_path(api, namespace_name, release_name)
    )
    fields["instance_slug"] = resolved(
        instance_slug or f"{namespace_name}-{release_name}",
        "explicit override" if instance_slug else "namespace and Galaxy release",
    )

    fields.update(_batch_runner(api, namespace_name, release_name))

    host = metadata.descriptor()
    fields["host_project"] = host["project"]
    fields["host_instance_id"] = host["instance_id"]
    fields["host_node_name"] = host["node_name"]
    fields["host_machine_type"] = host["machine_type"]
    fields["host_zone"] = host["zone"]
    fields["host_region"] = host["region"]
    if host["instance_id"].ok and host["machine_type"].ok:
        fields["baseline_policy_version"] = resolved(
            f"host-{host['instance_id'].value}", "discovered host descriptor"
        )
        notes.append(
            "the baseline describes the Galaxy server already running; placement is verified per "
            "observation before any job is reported as adding no compute charge"
        )
    else:
        fields["baseline_policy_version"] = unresolved(
            "the host descriptor is incomplete, so no baseline accounting policy is emitted"
        )
    fields.update(_baseline_execution(api, namespace_name, release_name))

    storage_class, storage_notes = _storage(api, namespace_name, release_name)
    fields["storage_class"] = storage_class
    notes.extend(storage_notes)

    if fields["batch_detected"].ok and fields["batch_detected"].value:
        if not fields["batch_project"].ok and host["project"].ok:
            notes.append(
                "a Batch runner is configured but names no project; the host VM's project is a "
                "fallback only, and Batch need not run there"
            )
    notes.append(
        "cloud reads use the credential this deployment's identity already has; probe results "
        "appear in the status report rather than being assumed"
    )
    return Discovery(fields=fields, notes=notes)
