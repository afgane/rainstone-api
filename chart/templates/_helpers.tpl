{{- define "rainstone.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rainstone.fullname" -}}
{{- $name := include "rainstone.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "rainstone.labels" -}}
app.kubernetes.io/name: {{ include "rainstone.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: galaxy
{{- end -}}

{{/* Per-component service accounts: web, collector and init are distinct. */}}
{{- define "rainstone.serviceAccountName" -}}
{{- $component := index . 1 -}}
{{- $root := index . 0 -}}
{{- $config := index $root.Values.serviceAccounts $component -}}
{{- if $root.Values.serviceAccounts.create -}}
{{- default (printf "%s-%s" (include "rainstone.fullname" $root) $component) $config.name -}}
{{- else -}}
{{- default "default" $config.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret holding a complete source DSN, when one is in use: a
preexisting one, or the one the provisioning mode publishes. */}}
{{- define "rainstone.sourceSecret" -}}
{{- default (printf "%s-source" (include "rainstone.fullname" .)) .Values.source.dsnSecret -}}
{{- end -}}

{{/*
The Galaxy account this deployment reports on. It is one account, so
`auth.workspaceOwner` is the answer unless an installation deliberately enrols a
different one.
*/}}
{{- define "rainstone.sharedAccount" -}}
{{- default .Values.auth.workspaceOwner .Values.source.sharedAccount -}}
{{- end -}}

{{- define "rainstone.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "rainstone.databaseSecret" -}}
{{- default (printf "%s-database" (include "rainstone.fullname" .)) .Values.database.existingSecret -}}
{{- end -}}

{{- define "rainstone.databaseSecretKey" -}}
{{- if .Values.database.existingSecret -}}{{ .Values.database.existingSecretKey }}{{- else -}}dsn{{- end -}}
{{- end -}}

{{/* Settings shared by the web and collector workloads. */}}
{{- define "rainstone.env" -}}
- name: RAINSTONE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "rainstone.databaseSecret" . }}
      key: {{ include "rainstone.databaseSecretKey" . }}
- name: RAINSTONE_AUTH_MODE
  value: {{ .Values.auth.mode | quote }}
{{- if .Values.auth.workspaceOwner }}
- name: RAINSTONE_WORKSPACE_OWNER_SOURCE_ID
  value: {{ .Values.auth.workspaceOwner | quote }}
{{- end }}
- name: RAINSTONE_WORKSPACE_INFRASTRUCTURE_VISIBLE
  value: {{ .Values.auth.workspaceInfrastructureVisible | quote }}
- name: RAINSTONE_TENANT_SLUG
  value: {{ required "instance.slug must be supplied by boot" .Values.instance.slug | quote }}
- name: RAINSTONE_TENANT_DISPLAY_NAME
  value: {{ .Values.instance.displayName | quote }}
- name: RAINSTONE_ROOT_PATH
  value: {{ .Values.basePath | quote }}
- name: RAINSTONE_DIAGNOSTICS_ENABLED
  value: {{ .Values.diagnosticsEnabled | quote }}
# Readiness waits for this release's own initialization, not only for a schema
# that an unchanged upgrade would already satisfy.
- name: RAINSTONE_INSTALLATION_ID
  value: {{ printf "%s-%d" .Release.Name (int .Release.Revision) | quote }}
- name: RAINSTONE_CATALOG_REFRESH_SECONDS
  value: {{ .Values.catalog.refreshSeconds | quote }}
- name: RAINSTONE_CATALOG_REQUIRE_SIGNATURE
  value: {{ .Values.catalog.requireSignature | quote }}
{{- if .Values.catalog.feedUrl }}
{{- if not .Values.catalog.trustedKeys }}
{{- fail "catalog.trustedKeys is required when catalog.feedUrl is set: a downloaded artifact is untrusted until its signature verifies" }}
{{- end }}
- name: RAINSTONE_CATALOG_FEED_URL
  value: {{ .Values.catalog.feedUrl | quote }}
{{- end }}
{{- if .Values.catalog.trustedKeys }}
- name: RAINSTONE_CATALOG_TRUSTED_KEYS
  value: {{ .Values.catalog.trustedKeys | quote }}
{{- end }}
{{- if .Values.baseline.policyVersion }}
- name: RAINSTONE_BASELINE_POLICY_VERSION
  value: {{ .Values.baseline.policyVersion | quote }}
- name: RAINSTONE_BASELINE_RESOURCE_UID
  value: {{ required "baseline.resourceUid is required with a baseline policy" .Values.baseline.resourceUid | quote }}
- name: RAINSTONE_BASELINE_MACHINE_TYPE
  value: {{ .Values.baseline.machineType | quote }}
- name: RAINSTONE_BASELINE_REGION
  value: {{ .Values.baseline.region | quote }}
- name: RAINSTONE_BASELINE_ZONE
  value: {{ .Values.baseline.zone | quote }}
- name: RAINSTONE_BASELINE_NODE_NAMES
  value: {{ .Values.baseline.nodeNames | quote }}
- name: RAINSTONE_BASELINE_DESTINATIONS
  value: {{ .Values.baseline.destinations | quote }}
- name: RAINSTONE_BASELINE_RUNNERS
  value: {{ .Values.baseline.runners | quote }}
{{- end }}
{{- end -}}

{{/* Whether Batch observation is on: `auto` follows the configured project. */}}
{{- define "rainstone.batchEnabled" -}}
{{- $batch := .Values.collector.gcpBatch -}}
{{- $mode := toString $batch.enabled -}}
{{- if eq $mode "auto" -}}
{{- if and $batch.project $batch.location -}}true{{- else -}}false{{- end -}}
{{- else if eq $mode "true" -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
The Galaxy source connection, for the collector and for initialization.

The default profile references Galaxy's existing credential Secret and builds
the URL from components, so no DSN is assembled by hand and no credential is
rendered into a manifest. A preexisting DSN Secret and the optional provisioned
reader both arrive as one secret reference instead.
*/}}
{{- define "rainstone.sourceEnv" -}}
{{- $source := .Values.source -}}
{{- $dsnSecret := include "rainstone.sourceDsnSecret" . -}}
{{- if $dsnSecret }}
- name: RAINSTONE_GALAXY_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $dsnSecret }}
      key: {{ $source.dsnSecretKey }}
- name: RAINSTONE_GALAXY_SOURCE_PRIVILEGE
  value: {{ $source.provisionReader.enabled | ternary "provisioned-reader" "application-credential" }}
{{- else if $source.existingSecret }}
- name: RAINSTONE_GALAXY_DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ $source.existingSecret }}
      key: {{ $source.usernameKey }}
- name: RAINSTONE_GALAXY_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $source.existingSecret }}
      key: {{ $source.passwordKey }}
- name: RAINSTONE_GALAXY_DB_HOST
  value: {{ required "source.host is required with source.existingSecret" $source.host | quote }}
- name: RAINSTONE_GALAXY_DB_PORT
  value: {{ $source.port | quote }}
- name: RAINSTONE_GALAXY_DB_NAME
  value: {{ $source.database | quote }}
{{- if $source.sslMode }}
- name: RAINSTONE_GALAXY_DB_SSLMODE
  value: {{ $source.sslMode | quote }}
{{- end }}
{{- if $source.options }}
- name: RAINSTONE_GALAXY_DB_OPTIONS
  value: {{ $source.options | quote }}
{{- end }}
# Galaxy's application credential retains Galaxy's write privileges. Read-only
# transactions, allowlisted statements, a statement timeout and a bounded pool
# are enforced by Rainstone, not by the database; the status report says so.
- name: RAINSTONE_GALAXY_SOURCE_PRIVILEGE
  value: application-credential
{{- end }}
- name: RAINSTONE_GALAXY_STATEMENT_TIMEOUT
  value: {{ $source.statementTimeout | quote }}
{{- if $source.deploymentEvidence }}
- name: RAINSTONE_SOURCE_DEPLOYMENT_EVIDENCE
  value: {{ $source.deploymentEvidence | quote }}
{{- end }}
{{- end -}}

{{- define "rainstone.sourceDsnSecret" -}}
{{- if or .Values.source.dsnSecret .Values.source.provisionReader.enabled -}}
{{- include "rainstone.sourceSecret" . -}}
{{- end -}}
{{- end -}}

{{/* Settings the collector alone needs, beyond the source connection. */}}
{{- define "rainstone.collectorEnv" -}}
{{- include "rainstone.sourceEnv" . }}
- name: RAINSTONE_COLLECTOR_HEARTBEAT_PATH
  value: /tmp/rainstone-collector.heartbeat
- name: RAINSTONE_GALAXY_BATCH_SIZE
  value: {{ .Values.source.batchSize | quote }}
- name: RAINSTONE_GALAXY_REPLAY_OVERLAP_SECONDS
  value: {{ .Values.source.replayOverlapSeconds | quote }}
- name: RAINSTONE_COLLECT_INTERVAL_SECONDS
  value: {{ .Values.collector.intervalSeconds | quote }}
- name: RAINSTONE_COLLECT_BATCH_REFRESH_SECONDS
  value: {{ .Values.collector.batchRefreshSeconds | quote }}
- name: RAINSTONE_KUBERNETES_ENABLED
  value: {{ .Values.collector.kubernetes.enabled | quote }}
- name: RAINSTONE_KUBERNETES_NAMESPACE
  value: {{ .Values.collector.kubernetes.namespace | default .Release.Namespace | quote }}
- name: RAINSTONE_GCP_BATCH_ENABLED
  value: {{ include "rainstone.batchEnabled" . | quote }}
{{- if eq (include "rainstone.batchEnabled" .) "true" }}
- name: RAINSTONE_GCP_PROJECT
  value: {{ required "collector.gcpBatch.project is required" .Values.collector.gcpBatch.project | quote }}
- name: RAINSTONE_GCP_LOCATION
  value: {{ required "collector.gcpBatch.location is required" .Values.collector.gcpBatch.location | quote }}
- name: RAINSTONE_GCP_ENRICH_COMPUTE
  value: {{ .Values.collector.gcpBatch.enrichCompute | quote }}
- name: RAINSTONE_GCP_ENRICH_LOGGING
  value: {{ .Values.collector.gcpBatch.enrichLogging | quote }}
{{- end }}
{{- end -}}
