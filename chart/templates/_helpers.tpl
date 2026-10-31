{{/*
Expand the name of the chart.
*/}}
{{- define "lumen.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this.
*/}}
{{- define "lumen.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "lumen.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Name of the migration Job: <fullname>-migrate-<revision>. The fullname is
shortened so the whole name, with every digit of the revision, stays within the
63-character limit.
*/}}
{{- define "lumen.migrateJobName" -}}
{{- $suffix := printf "-migrate-%d" (int .Release.Revision) -}}
{{- printf "%s%s" (include "lumen.fullname" . | trunc (int (sub 63 (len $suffix))) | trimSuffix "-") $suffix -}}
{{- end }}

{{/*
app.kubernetes.io/name label of the migration Job's pod. It must differ from the
Deployment's selector label, or the Service would send traffic to the Job. A
name that already ends in "-migrate" can truncate to the same value, so that
case gets "-job" instead.
*/}}
{{- define "lumen.migrateName" -}}
{{- $name := include "lumen.name" . -}}
{{- $candidate := printf "%s-migrate" ($name | trunc 55 | trimSuffix "-") -}}
{{- if eq $candidate $name -}}
{{- $candidate = printf "%s-job" ($name | trunc 59 | trimSuffix "-") -}}
{{- end -}}
{{- $candidate -}}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "lumen.labels" -}}
helm.sh/chart: {{ include "lumen.chart" . }}
{{ include "lumen.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "lumen.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lumen.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the Secret holding main app credentials.
Resolves to existingSecret if set, otherwise the chart-managed secret.
*/}}
{{- define "lumen.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "lumen.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Secret name for DATABASE_URL.
*/}}
{{- define "lumen.databaseSecretName" -}}
{{- if .Values.postgresql.existingSecret -}}
{{- .Values.postgresql.existingSecret -}}
{{- else -}}
{{- include "lumen.secretName" . -}}
{{- end -}}
{{- end }}

{{/*
Secret key for DATABASE_URL.
*/}}
{{- define "lumen.databaseSecretKey" -}}
{{- if .Values.postgresql.existingSecret -}}
{{- .Values.postgresql.existingSecretKey | default "database-url" -}}
{{- else -}}
{{- "database-url" -}}
{{- end -}}
{{- end }}

{{/*
Compute the database URL (written into the chart-managed Secret).
Service name: <fullname>-postgresql (matches templates/postgresql/)
When postgresql.auth.existingSecret is set the password is unknown to the chart;
the user must supply the full URL via postgresql.url or set existingSecret on the app secret.
*/}}
{{- define "lumen.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
{{- if .Values.postgresql.auth.existingSecret -}}
{{- if .Values.postgresql.url -}}
{{- .Values.postgresql.url -}}
{{- else -}}
{{- fail "postgresql.auth.existingSecret is set but postgresql.url is empty: the chart cannot build DATABASE_URL. Set postgresql.url with the full connection string." -}}
{{- end -}}
{{- else -}}
{{- printf "postgresql://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password (include "lumen.fullname" .) .Values.postgresql.auth.database -}}
{{- end -}}
{{- else -}}
{{- .Values.postgresql.url -}}
{{- end -}}
{{- end }}

{{/*
Compute the Redis storage URL for embedding in config.yaml.
Service name: <fullname>-redis (matches templates/redis/)
Returns empty string when redis is not configured → in-memory rate limiting.
*/}}
{{- define "lumen.redisUrl" -}}
{{- if .Values.redis.enabled -}}
{{- $port := .Values.redis.port | default 6379 -}}
{{- if and .Values.redis.auth.enabled .Values.redis.auth.password -}}
{{- printf "redis://:%s@%s-redis:%d/0" .Values.redis.auth.password (include "lumen.fullname" .) (int $port) -}}
{{- else -}}
{{- printf "redis://%s-redis:%d/0" (include "lumen.fullname" .) (int $port) -}}
{{- end -}}
{{- else if .Values.redis.url -}}
{{- .Values.redis.url -}}
{{- end -}}
{{- end }}

{{/*
Image reference for the Lumen app.
*/}}
{{- define "lumen.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}

{{/*
Environment of the Lumen app container. Shared by the app container, the
wait-for-migrations gate initContainer and the migration Job, so all three
resolve the database URL and config exactly the same way.
*/}}
{{- define "lumen.appEnv" -}}
- name: UV_CACHE_DIR
  value: /tmp/uv-cache
- name: CONFIG_YAML
  value: /app/config/config.yaml
- name: LUMEN_REPLICAS
  value: {{ .Values.replicaCount | quote }}
- name: WEB_CONCURRENCY
  value: {{ .Values.wsgiProcesses | quote }}
- name: LUMEN_WSGI_WORKERS
  value: {{ .Values.wsgiWorkers | quote }}
- name: LUMEN_WSGI_SEND_TIMEOUT
  value: {{ .Values.wsgiSendTimeout | quote }}
{{- if and .Values.api.prometheus.enabled .Values.api.prometheus.multiprocDir }}
- name: PROMETHEUS_MULTIPROC_DIR
  value: {{ .Values.api.prometheus.multiprocDir | quote }}
{{- end }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "lumen.databaseSecretName" . }}
      key: {{ include "lumen.databaseSecretKey" . }}
- name: LUMEN_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lumen.secretName" . }}
      key: secret-key
- name: LUMEN_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lumen.secretName" . }}
      key: encryption-key
- name: OAUTH2_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "lumen.secretName" . }}
      key: oauth2-client-id
- name: OAUTH2_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "lumen.secretName" . }}
      key: oauth2-client-secret
{{- end }}

{{/*
Volume mounts of the Lumen app container, also used by the wait-for-migrations
gate initContainer.
*/}}
{{- define "lumen.appVolumeMounts" -}}
- name: config
  mountPath: /app/config
  readOnly: true
{{- range $name, $theme := .Values.config.themes }}
- name: theme-{{ $name }}
  mountPath: {{ printf "/app/themes/%s" $name }}
  readOnly: true
{{- end }}
{{- include "lumen.prometheusMount" . }}
{{- end }}

{{/*
Volume mounts of the migration Job: the app's, minus the theme PVCs. The upgrade
renders no templates (an unknown theme falls back to "default"), and a
ReadWriteOnce PVC held by the running app pod would keep the Job's pod from
starting on another node.
*/}}
{{- define "lumen.migrateVolumeMounts" -}}
- name: config
  mountPath: /app/config
  readOnly: true
{{- include "lumen.prometheusMount" . }}
{{- end }}

{{/*
Mount for PROMETHEUS_MULTIPROC_DIR, which is in the shared env whenever it is set:
prometheus_client raises if that directory does not exist.
*/}}
{{- define "lumen.prometheusMount" -}}
{{- if and .Values.api.prometheus.enabled .Values.api.prometheus.multiprocDir }}
- name: prometheus-multiproc
  mountPath: {{ .Values.api.prometheus.multiprocDir }}
{{- end }}
{{- end }}

{{/*
Container securityContext of the Lumen app container.
*/}}
{{- define "lumen.appSecurityContext" -}}
{{- if .Values.securityContext -}}
{{- toYaml .Values.securityContext }}
{{- else -}}
runAsNonRoot: true
runAsUser: 1000
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end }}
{{- end }}

{{/*
Resources of the Lumen app container.
*/}}
{{- define "lumen.appResources" -}}
{{- if .Values.resources -}}
{{- toYaml .Values.resources }}
{{- else -}}
requests:
  cpu: 250m
  memory: 512Mi
limits:
  cpu: 1
  memory: 512Mi
{{- end }}
{{- end }}

{{/*
initContainer that waits until the database accepts TCP connections. Used by the
Deployment and the migration Job.
*/}}
{{- define "lumen.waitForDbInitContainer" -}}
- name: wait-for-db
  image: busybox:1.36
  securityContext:
    runAsNonRoot: true
    runAsUser: 65534
    allowPrivilegeEscalation: false
    capabilities:
      drop: [ALL]
  command:
    - sh
    - -c
    - |
      DB_HOST=$(echo $DATABASE_URL | sed 's|.*@\([^:/]*\).*|\1|')
      DB_PORT=$(echo $DATABASE_URL | sed 's|.*:\([0-9]*\)/.*|\1|')
      DB_PORT=${DB_PORT:-5432}
      echo "Waiting for $DB_HOST:$DB_PORT..."
      until nc -z "$DB_HOST" "$DB_PORT"; do
        echo "database not ready, sleeping 2s"
        sleep 2
      done
      echo "database is reachable"
  env:
    - name: DATABASE_URL
      valueFrom:
        secretKeyRef:
          name: {{ include "lumen.databaseSecretName" . }}
          key: {{ include "lumen.databaseSecretKey" . }}
{{- end }}

{{/*
Pod-level scheduling and identity settings shared by the Deployment and the
migration Job.
*/}}
{{- define "lumen.podSpecCommon" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if .Values.serviceAccount.create }}
serviceAccountName: {{ .Values.serviceAccount.name | default (include "lumen.fullname" .) }}
{{- end }}
{{- with .Values.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
