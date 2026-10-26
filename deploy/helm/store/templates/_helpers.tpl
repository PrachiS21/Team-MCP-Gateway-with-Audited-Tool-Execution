{{/* Labels put on every resource, so `kubectl get all -l app.kubernetes.io/part-of=store` finds them all. */}}
{{- define "store.labels" -}}
app.kubernetes.io/part-of: store
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/* The public URL, e.g. http://shop.192.168.1.11.sslip.io */}}
{{- define "store.publicUrl" -}}
{{ .Values.scheme }}://{{ required "set .Values.host in a values-<env>.yaml" .Values.host }}
{{- end }}

{{/* "repository:tag" for an entry of .Values.images, e.g. {{ include "store.image" .Values.images.mcp }} */}}
{{- define "store.image" -}}
{{ .repository }}:{{ .tag }}
{{- end }}

{{/* Where the MCP server reaches WordPress: the in-cluster Service, not the public URL. */}}
{{- define "store.internalUrl" -}}
http://wordpress.{{ .Release.Namespace }}.svc.cluster.local
{{- end }}

{{/* The storage class line, left out entirely when empty so the cluster default applies. */}}
{{- define "store.storageClass" -}}
{{- if .Values.storage.className }}
storageClassName: {{ .Values.storage.className | quote }}
{{- end }}
{{- end }}

{{/* Environment shared by the WordPress pod and the setup Job: how to reach the database.
     wp-config.php reads these at RUN time, so BOTH need them (see the note in docker-compose.yml). */}}
{{- define "store.dbEnv" -}}
- { name: WORDPRESS_DB_HOST, value: "mariadb:3306" }
- { name: WORDPRESS_DB_NAME, value: "wordpress" }
- { name: WORDPRESS_DB_USER, value: "wordpress" }
- name: WORDPRESS_DB_PASSWORD
  valueFrom: { secretKeyRef: { name: {{ .Values.secrets.db }}, key: DB_PASSWORD } }
{{- end }}
