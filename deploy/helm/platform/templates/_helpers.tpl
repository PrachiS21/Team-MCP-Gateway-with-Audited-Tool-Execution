{{/* Labels put on every resource, so `kubectl get all -l app.kubernetes.io/part-of=platform` finds them all. */}}
{{- define "platform.labels" -}}
app.kubernetes.io/part-of: platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/* The public URL, e.g. http://shop.192.168.1.11.sslip.io */}}
{{- define "platform.publicUrl" -}}
{{ .Values.scheme }}://{{ required "set .Values.host in a values-<env>.yaml" .Values.host }}
{{- end }}

{{- define "platform.image" -}}
{{ .repository }}:{{ .tag }}
{{- end }}

{{/* The seed file the gateway reads at every start. Built from .Values.servers; it names environment variables
     for credentials and never contains a secret (the gateway refuses a file that does). */}}
{{- define "platform.seed" -}}
{{- $servers := list -}}
{{- range .Values.servers -}}
{{- $e := dict "slug" .slug "name" .name "url" .url -}}
{{- if .credentialType }}{{ $_ := set $e "credential_type" .credentialType }}{{ end -}}
{{- if .credentialEnv }}{{ $_ := set $e "credential_env" .credentialEnv }}{{ end -}}
{{- if .credentialHeader }}{{ $_ := set $e "credential_header" .credentialHeader }}{{ end -}}
{{- if .timeout }}{{ $_ := set $e "timeout" .timeout }}{{ end -}}
{{- $servers = append $servers $e -}}
{{- end -}}
{{- dict "servers" $servers | toPrettyJson -}}
{{- end }}
