#!/bin/sh
set -eu

destination="${1:-deploy/dev-secrets}"
tenant_id="${MEMORY_SEED_TENANT_ID:-tenant-a}"
python_command="${PYTHON:-python3.12}"
umask 077
mkdir -p "$destination/tenants/$tenant_id"
tenant_secrets_gid="$(id -g)"
chgrp "$tenant_secrets_gid" "$destination/tenants" "$destination/tenants/$tenant_id"
chmod 0750 "$destination/tenants" "$destination/tenants/$tenant_id"

ensure_secret() {
  path="$1"
  if [ ! -e "$path" ]; then
    openssl rand -hex 32 > "$path"
    chmod 0600 "$path"
  fi
}

ensure_secret "$destination/postgres_password"
ensure_secret "$destination/postgres_admin_password"
ensure_secret "$destination/postgres_exporter_password"
ensure_secret "$destination/rabbitmq_password"
ensure_secret "$destination/grafana_admin_password"
ensure_secret "$destination/telemetry_hmac_key"
ensure_secret "$destination/tenants/$tenant_id/postgres_password"
ensure_secret "$destination/tenants/$tenant_id/neo4j_password"
chgrp "$tenant_secrets_gid" "$destination/tenants/$tenant_id/postgres_password" \
  "$destination/tenants/$tenant_id/neo4j_password"
chmod 0640 "$destination/tenants/$tenant_id/postgres_password" \
  "$destination/tenants/$tenant_id/neo4j_password"

tenant_telemetry_ref="$(
  PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" \
    MEMORY_TELEMETRY_HMAC_KEY_FILE="$destination/telemetry_hmac_key" \
    "$python_command" scripts/telemetry-reference.py "$tenant_id"
)"
printf '%s\n' "memory.tenant_ref=$tenant_telemetry_ref" > "$destination/$tenant_id.labels"
chmod 0600 "$destination/$tenant_id.labels"

printf '%s\n' "Created missing development secrets under $destination."
printf '%s\n' "Existing secrets were preserved."
