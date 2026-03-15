{{- define "tf-registry-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tf-registry-api.fullname" -}}
{{- default (include "tf-registry-api.name" .) .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tf-registry-api.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "tf-registry-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app: tf-registry-api
{{- end }}

{{- define "tf-registry-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tf-registry-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app: tf-registry-api
{{- end }}
