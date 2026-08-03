#!/bin/sh
set -eu

# Compose file secrets are bind mounts on Linux and retain their host ownership and
# mode. Copy them while this wrapper is still root so the upstream entrypoint and our
# initialization scripts can read them after dropping to the postgres user.
runtime_directory="/run/coengram-postgres-secrets"
mkdir -p "$runtime_directory"
chown root:postgres "$runtime_directory"
chmod 0750 "$runtime_directory"

copy_secret() {
  source_path="$1"
  secret_name="$2"
  target_path="$runtime_directory/$secret_name"
  if [ ! -f "$source_path" ]; then
    printf '%s\n' "Required PostgreSQL secret file is unavailable: $secret_name" >&2
    exit 1
  fi
  rm -f "$target_path"
  cp "$source_path" "$target_path"
  chown postgres:postgres "$target_path"
  chmod 0400 "$target_path"
}

if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
  copy_secret "$POSTGRES_PASSWORD_FILE" postgres_admin_password
  POSTGRES_PASSWORD_FILE="$runtime_directory/postgres_admin_password"
  export POSTGRES_PASSWORD_FILE
fi

control_source="${POSTGRES_CONTROL_PASSWORD_FILE:-/run/secrets/postgres_password}"
if [ -e "$control_source" ]; then
  copy_secret "$control_source" postgres_password
  POSTGRES_CONTROL_PASSWORD_FILE="$runtime_directory/postgres_password"
  export POSTGRES_CONTROL_PASSWORD_FILE
fi

exporter_source="${POSTGRES_EXPORTER_PASSWORD_FILE:-/run/secrets/postgres_exporter_password}"
if [ -e "$exporter_source" ]; then
  copy_secret "$exporter_source" postgres_exporter_password
  POSTGRES_EXPORTER_PASSWORD_FILE="$runtime_directory/postgres_exporter_password"
  export POSTGRES_EXPORTER_PASSWORD_FILE
fi

tenant_source="${POSTGRES_TENANT_PASSWORD_FILE:-/run/secrets/tenant_postgres_password}"
if [ -e "$tenant_source" ]; then
  copy_secret "$tenant_source" tenant_postgres_password
  POSTGRES_TENANT_PASSWORD_FILE="$runtime_directory/tenant_postgres_password"
  export POSTGRES_TENANT_PASSWORD_FILE
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
