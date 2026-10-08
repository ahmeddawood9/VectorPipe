{{- define "vectorpipe.image" -}}
{{ required "image.repository is required (generate values-aws.yaml)" .Values.image.repository }}:{{ required "image.tag is required (--set image.tag=<git sha>)" .Values.image.tag }}
{{- end }}

{{- define "vectorpipe.labels" -}}
app.kubernetes.io/name: vectorpipe
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "vectorpipe.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.securityContext.runAsUser }}
{{- end }}

{{/* DB_USER and DB_PASSWORD_ENCODED must come BEFORE DATABASE_URL: $(VAR) only expands earlier variables. */}}
{{- define "vectorpipe.dbEnv" -}}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: db-credentials
      key: DB_USER
- name: DB_PASSWORD_ENCODED
  valueFrom:
    secretKeyRef:
      name: db-credentials
      key: DB_PASSWORD_ENCODED
- name: DATABASE_URL
  value: "postgresql://$(DB_USER):$(DB_PASSWORD_ENCODED)@{{ required "db.host is required" .Values.db.host }}:{{ .Values.db.port }}/{{ .Values.db.name }}?sslmode=require"
{{- end }}
