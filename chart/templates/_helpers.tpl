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
Compute the OAuth2 redirect URI.
Falls back to https://<ingress.host>/callback or https://<gateway.hostname>/callback.
*/}}
{{- define "lumen.redirectUri" -}}
{{- if .Values.oauth2.redirectUri -}}
{{- .Values.oauth2.redirectUri -}}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- printf "https://%s/callback" .Values.ingress.host -}}
{{- else if and .Values.gateway.enabled .Values.gateway.hostname -}}
{{- printf "https://%s/callback" .Values.gateway.hostname -}}
{{- else -}}
{{- "" -}}
{{- end -}}
{{- end }}

{{/*
Image reference for the Lumen app.
*/}}
{{- define "lumen.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}
