#!/bin/sh
set -eu

tenant_id="${MEMORY_SEED_TENANT_ID:-tenant-a}"
case "$tenant_id" in
  *[!a-z0-9-]*|'')
    printf '%s\n' "MEMORY_SEED_TENANT_ID is invalid" >&2
    exit 1
    ;;
esac
tenant_database="tenant_$(printf '%s' "$tenant_id" | tr '-' '_')"
tenant_role="${tenant_database}_rw"
tenant_password_file="${POSTGRES_TENANT_PASSWORD_FILE:-/run/secrets/tenant_postgres_password}"
tenant_password="$(tr -d '\r\n' < "$tenant_password_file")"

case "$tenant_password" in
  *[!A-Za-z0-9_-]*|'')
    printf '%s\n' "Tenant PostgreSQL password has unsupported characters" >&2
    exit 1
    ;;
esac

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '${tenant_role}', '${tenant_password}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${tenant_role}') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', '${tenant_database}', '${tenant_role}')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${tenant_database}') \gexec
REVOKE CONNECT ON DATABASE "${tenant_database}" FROM PUBLIC;
GRANT CONNECT ON DATABASE "${tenant_database}" TO "${tenant_role}";
SQL
