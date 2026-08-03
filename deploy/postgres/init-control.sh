#!/bin/sh
set -eu

control_user="${POSTGRES_CONTROL_USER:-memory_control}"
control_password_file="${POSTGRES_CONTROL_PASSWORD_FILE:-/run/secrets/postgres_password}"
control_password="$(tr -d '\r\n' < "$control_password_file")"
exporter_user="${POSTGRES_EXPORTER_USER:-memory_metrics}"
exporter_password_file="${POSTGRES_EXPORTER_PASSWORD_FILE:-/run/secrets/postgres_exporter_password}"
exporter_password="$(tr -d '\r\n' < "$exporter_password_file")"

case "$control_user" in
  *[!a-z0-9_]*|'')
    printf '%s\n' "POSTGRES_CONTROL_USER is invalid" >&2
    exit 1
    ;;
esac
case "$control_password" in
  *[!A-Za-z0-9_-]*|'')
    printf '%s\n' "Control PostgreSQL password has unsupported characters" >&2
    exit 1
    ;;
esac
case "$exporter_user" in
  *[!a-z0-9_]*|'')
    printf '%s\n' "PostgreSQL exporter user is invalid" >&2
    exit 1
    ;;
esac
case "$exporter_password" in
  *[!A-Za-z0-9_-]*|'')
    printf '%s\n' "PostgreSQL exporter password has unsupported characters" >&2
    exit 1
    ;;
esac

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '${control_user}', '${control_password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${control_user}') \gexec
ALTER DATABASE "${POSTGRES_DB}" OWNER TO "${control_user}";
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '${exporter_user}', '${exporter_password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${exporter_user}') \gexec
GRANT pg_monitor TO "${exporter_user}";
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${exporter_user}";
SQL
