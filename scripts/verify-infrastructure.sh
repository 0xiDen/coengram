#!/bin/sh
set -eu

run_id="$$"
repository_root="$(pwd -P)"
postgres_container="memory-verify-postgres-$run_id"
postgres_image="memory-verify-postgres:$run_id"
rabbit_container="memory-verify-rabbit-$run_id"
rabbit_image="rabbitmq:4.1-management-alpine@sha256:218487e5c22d92a990c7ad42133b7e8784c4956f8d395cd89ce2cac6abc8a8fa"
neo4j_container="memory-verify-neo4j-$run_id"
neo4j_second_container="memory-verify-neo4j-second-$run_id"
neo4j_image="neo4j:5.26.28-community@sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9"
platform_image="memory-verify-platform:$run_id"
postgres_admin_password="verify-postgres-admin-password"
postgres_password="verify-postgres-control-password"
postgres_exporter_password="verify-postgres-exporter-password"
tenant_postgres_password="verify-tenant-postgres-password"
rabbit_password="verify-rabbit-password"
neo4j_password="verify-neo4j-password"
neo4j_second_password="verify-neo4j-second-password"
backup_neo4j_password="verify-backup-neo4j-password"
secret_directory="$(mktemp -d "${TMPDIR:-/tmp}/memory-infra-secrets.XXXXXX")"
backup_tenant_secrets_root="$secret_directory/backup-tenants"
backup_tenant_secrets_directory="$backup_tenant_secrets_root/tenant-a"
backup_source_compose="$repository_root/tests/integration/backup-source.compose.yaml"
backup_source_started=false
backup_source_volume="memory-tenant-tenant-a-neo4j-data"
backup_source_network="memory-tenant-tenant-a_default"

reserve_loopback_port() {
  python3.12 -c \
    "import socket; listener = socket.socket(); listener.bind(('127.0.0.1', 0)); print(listener.getsockname()[1]); listener.close()"
}

postgres_port="$(reserve_loopback_port)"
rabbit_port="$(reserve_loopback_port)"
neo4j_port="$(reserve_loopback_port)"
neo4j_second_port="$(reserve_loopback_port)"
backup_neo4j_port="$(reserve_loopback_port)"

umask 077
printf '%s\n' "$postgres_admin_password" > "$secret_directory/postgres_admin_password"
printf '%s\n' "$postgres_password" > "$secret_directory/postgres_password"
printf '%s\n' "$postgres_exporter_password" > "$secret_directory/postgres_exporter_password"
printf '%s\n' "$tenant_postgres_password" > "$secret_directory/tenant_postgres_password"
printf '%s\n' "$rabbit_password" > "$secret_directory/rabbitmq_password"
mkdir -p "$backup_tenant_secrets_directory"
printf '%s\n' "$tenant_postgres_password" > "$backup_tenant_secrets_directory/postgres_password"
printf '%s\n' "$backup_neo4j_password" > "$backup_tenant_secrets_directory/neo4j_password"
chmod 0640 "$backup_tenant_secrets_directory/postgres_password"
age-keygen --output "$secret_directory/backup-age-identity" 2>/dev/null
age-keygen --output "$secret_directory/backup-wrong-age-identity" 2>/dev/null
age-keygen -y "$secret_directory/backup-age-identity" > "$secret_directory/backup-age-recipients"
chmod 0600 "$secret_directory/backup-age-identity" \
  "$secret_directory/backup-wrong-age-identity" \
  "$secret_directory/backup-age-recipients"

# This fixture deliberately exercises production's canonical Tenant resource names.
# Refuse to run when any exact name is already owned outside this test invocation.
if docker volume inspect "$backup_source_volume" >/dev/null 2>&1 \
  || docker network inspect "$backup_source_network" >/dev/null 2>&1 \
  || [ -n "$(docker ps --all --quiet --filter label=com.docker.compose.project=memory-tenant-tenant-a)" ]; then
  printf '%s\n' "Canonical tenant-a Docker resources already exist; refusing integration cleanup" >&2
  exit 1
fi

cleanup() {
  if [ "$backup_source_started" = true ]; then
    env TENANT_ID=tenant-a \
      TENANT_SECRETS_DIR="$backup_tenant_secrets_directory" \
      BACKUP_NEO4J_PORT="$backup_neo4j_port" \
      docker compose --project-name memory-tenant-tenant-a \
      --file "$backup_source_compose" down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  docker rm -f -v "$postgres_container" "$rabbit_container" "$neo4j_container" "$neo4j_second_container" >/dev/null 2>&1 || true
  docker image rm -f "$postgres_image" >/dev/null 2>&1 || true
  docker image rm -f "$platform_image" >/dev/null 2>&1 || true
  rm -f "$secret_directory/tenant-group-read/postgres_password"
  rmdir "$secret_directory/tenant-group-read" 2>/dev/null || true
  rm -f "$secret_directory/postgres_admin_password" "$secret_directory/postgres_password" "$secret_directory/postgres_exporter_password" "$secret_directory/tenant_postgres_password" "$secret_directory/rabbitmq_password"
  rm -f "$backup_tenant_secrets_directory/postgres_password" \
    "$backup_tenant_secrets_directory/neo4j_password" \
    "$secret_directory/backup-age-identity" \
    "$secret_directory/backup-wrong-age-identity" \
    "$secret_directory/backup-age-recipients" \
    "$secret_directory/backup-embedding-vector.json"
  rmdir "$backup_tenant_secrets_directory" 2>/dev/null || true
  rmdir "$backup_tenant_secrets_root" 2>/dev/null || true
  rmdir "$secret_directory" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

docker build --quiet --target local --tag "$postgres_image" "$repository_root/deploy/postgres" >/dev/null
docker run --detach --name "$postgres_container" -p "127.0.0.1:$postgres_port:5432" \
  -e POSTGRES_USER=memory_admin \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_admin_password \
  -e POSTGRES_DB=memory_control \
  -e POSTGRES_CONTROL_USER=memory_control \
  -e POSTGRES_EXPORTER_USER=memory_metrics \
  -e MEMORY_SEED_TENANT_ID=tenant-a \
  -v "$secret_directory/postgres_admin_password:/run/secrets/postgres_admin_password:ro" \
  -v "$secret_directory/postgres_password:/run/secrets/postgres_password:ro" \
  -v "$secret_directory/postgres_exporter_password:/run/secrets/postgres_exporter_password:ro" \
  -v "$secret_directory/tenant_postgres_password:/run/secrets/tenant_postgres_password:ro" \
  "$postgres_image" >/dev/null

docker run --detach --name "$rabbit_container" -p "127.0.0.1:$rabbit_port:5672" \
  -e MEMORY_RABBITMQ_SEED_USER=memory_verify \
  -e MEMORY_RABBITMQ_SEED_PASSWORD_FILE=/run/secrets/rabbitmq_password \
  -v "$secret_directory/rabbitmq_password:/run/secrets/rabbitmq_password:ro" \
  -v "$repository_root/deploy/rabbitmq/rabbitmq.conf:/etc/rabbitmq/rabbitmq.conf:ro" \
  -v "$repository_root/deploy/rabbitmq/entrypoint.sh:/etc/rabbitmq/memory-entrypoint.sh:ro" \
  -v "$repository_root/deploy/rabbitmq/enabled_plugins:/etc/rabbitmq/enabled_plugins:ro" \
  --entrypoint /bin/bash \
  "$rabbit_image" /etc/rabbitmq/memory-entrypoint.sh rabbitmq-server >/dev/null

docker run --detach --name "$neo4j_container" -p "127.0.0.1:$neo4j_port:7687" \
  -e NEO4J_AUTH="neo4j/$neo4j_password" \
  "$neo4j_image" >/dev/null

docker run --detach --name "$neo4j_second_container" -p "127.0.0.1:$neo4j_second_port:7687" \
  -e NEO4J_AUTH="neo4j/$neo4j_second_password" \
  "$neo4j_image" >/dev/null

backup_source_started=true
env TENANT_ID=tenant-a \
  TENANT_SECRETS_DIR="$backup_tenant_secrets_directory" \
  BACKUP_NEO4J_PORT="$backup_neo4j_port" \
  docker compose --project-name memory-tenant-tenant-a \
  --file "$backup_source_compose" up --detach --wait neo4j

attempt=0
until docker exec "$postgres_container" \
  psql -U memory_admin -d memory_control -Atc 'SELECT 1' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || {
    docker logs "$postgres_container" >&2
    printf '%s\n' "PostgreSQL did not become ready" >&2
    exit 1
  }
  sleep 1
done

role_flags="$(docker exec "$postgres_container" psql -U memory_admin -d memory_control -Atc \
  "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'memory_control'")"
[ "$role_flags" = "f|f|f|f|f" ] || {
  printf '%s\n' "memory_control unexpectedly has PostgreSQL cluster privileges" >&2
  exit 1
}
exporter_role_flags="$(docker exec "$postgres_container" psql -U memory_admin -d memory_control -Atc \
  "SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls, pg_has_role(r.oid, 'pg_monitor', 'MEMBER') FROM pg_roles AS r WHERE r.rolname = 'memory_metrics'")"
[ "$exporter_role_flags" = "f|f|f|f|f|t" ] || {
  printf '%s\n' "memory_metrics does not have the exact monitor-only role" >&2
  exit 1
}
if docker exec -e PGPASSWORD="$postgres_password" "$postgres_container" \
  psql -h 127.0.0.1 -U memory_control -d tenant_tenant_a -c 'SELECT 1' >/dev/null 2>&1; then
  printf '%s\n' "memory_control unexpectedly connected to a Tenant database" >&2
  exit 1
fi

attempt=0
until docker exec "$rabbit_container" rabbitmq-diagnostics -q check_running >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 90 ] || {
    docker logs "$rabbit_container" >&2
    printf '%s\n' "RabbitMQ did not become ready" >&2
    exit 1
  }
  sleep 1
done
rabbit_users="$(docker exec "$rabbit_container" rabbitmqctl list_users --formatter json | tr -d '[:space:]')"
[ "$rabbit_users" = '[{"user":"memory_verify","tags":[]}]' ] || {
  printf '%s\n' "RabbitMQ application user unexpectedly has management tags" >&2
  exit 1
}
rabbit_permissions="$(docker exec "$rabbit_container" rabbitmqctl list_permissions -p memory --formatter json | tr -d '[:space:]')"
[ "$rabbit_permissions" = '[{"user":"memory_verify","configure":"^memory[.]","write":"^memory[.]","read":"^memory[.]"}]' ] || {
  printf '%s\n' "RabbitMQ application permissions do not match the private topology" >&2
  exit 1
}

attempt=0
until docker exec "$neo4j_container" cypher-shell -u neo4j -p "$neo4j_password" 'RETURN 1' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 120 ] || { printf '%s\n' "Neo4j did not become ready" >&2; exit 1; }
  sleep 1
done

attempt=0
until docker exec "$neo4j_second_container" cypher-shell -u neo4j -p "$neo4j_second_password" 'RETURN 1' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 120 ] || { printf '%s\n' "Second Neo4j did not become ready" >&2; exit 1; }
  sleep 1
done

# Exercise the exact production image user against the host group ownership model.
tenant_secrets_gid="$(id -g)"
mkdir "$secret_directory/tenant-group-read"
printf '%s\n' "group-readable-value" > "$secret_directory/tenant-group-read/postgres_password"
chgrp "$tenant_secrets_gid" "$secret_directory" "$secret_directory/tenant-group-read" \
  "$secret_directory/tenant-group-read/postgres_password"
chmod 0750 "$secret_directory" "$secret_directory/tenant-group-read"
chmod 0640 "$secret_directory/tenant-group-read/postgres_password"
docker build --quiet --tag "$platform_image" "$repository_root" >/dev/null
docker run --rm --user 10001:10001 --group-add "$tenant_secrets_gid" --read-only \
  -v "$secret_directory:/run/secrets/tenants:ro" \
  "$platform_image" python -c \
  "from pathlib import Path; assert Path('/run/secrets/tenants/tenant-group-read/postgres_password').read_text().strip() == 'group-readable-value'"

# Prove the released image can embed without network access or a writable model cache.
docker run --rm --user 10001:10001 --network none --read-only \
  --tmpfs /tmp:size=64m,mode=1777 \
  "$platform_image" python -c \
  "from sentence_transformers import SentenceTransformer; model = SentenceTransformer('/opt/coengram/models/bge-small-en-v1.5', local_files_only=True); assert model.encode(['offline restore probe']).shape == (1, 384)"

# Seed the source graph with the exact offline embedding used by the isolated verifier.
docker run --rm --user 10001:10001 --network none --read-only \
  --tmpfs /tmp:size=64m,mode=1777 \
  "$platform_image" python -c \
  "import json; from sentence_transformers import SentenceTransformer; model = SentenceTransformer('/opt/coengram/models/bge-small-en-v1.5', local_files_only=True); print(json.dumps(model.encode(['restore availability verification'], normalize_embeddings=True)[0].tolist()))" \
  > "$secret_directory/backup-embedding-vector.json"
chmod 0600 "$secret_directory/backup-embedding-vector.json"

CONTROL_DATABASE_URL="postgresql://memory_control:$postgres_password@127.0.0.1:$postgres_port/memory_control" \
POSTGRES_ADMIN_URL="postgresql://memory_admin:$postgres_admin_password@127.0.0.1:$postgres_port/postgres" \
TENANT_POSTGRES_HOST="127.0.0.1" \
TENANT_POSTGRES_PORT="$postgres_port" \
TENANT_DATABASE_URL="postgresql://tenant_tenant_a_rw:$tenant_postgres_password@127.0.0.1:$postgres_port/tenant_tenant_a" \
AMQP_URL="amqp://memory_verify:$rabbit_password@127.0.0.1:$rabbit_port/memory" \
NEO4J_TEST_URI="bolt://127.0.0.1:$neo4j_port" \
NEO4J_TEST_PASSWORD="$neo4j_password" \
RABBITMQ_TEST_CONTAINER="$rabbit_container" \
NEO4J_TEST_CONTAINER="$neo4j_container" \
NEO4J_SECOND_TEST_URI="bolt://127.0.0.1:$neo4j_second_port" \
NEO4J_SECOND_TEST_PASSWORD="$neo4j_second_password" \
python3.12 -m pytest tests/integration/test_release_fault_recovery.py -q

CONTROL_DATABASE_URL="postgresql://memory_control:$postgres_password@127.0.0.1:$postgres_port/memory_control" \
POSTGRES_ADMIN_URL="postgresql://memory_admin:$postgres_admin_password@127.0.0.1:$postgres_port/postgres" \
TENANT_POSTGRES_HOST="127.0.0.1" \
TENANT_POSTGRES_PORT="$postgres_port" \
TENANT_DATABASE_URL="postgresql://tenant_tenant_a_rw:$tenant_postgres_password@127.0.0.1:$postgres_port/tenant_tenant_a" \
AMQP_URL="amqp://memory_verify:$rabbit_password@127.0.0.1:$rabbit_port/memory" \
NEO4J_TEST_URI="bolt://127.0.0.1:$neo4j_port" \
NEO4J_TEST_PASSWORD="$neo4j_password" \
NEO4J_SECOND_TEST_URI="bolt://127.0.0.1:$neo4j_second_port" \
NEO4J_SECOND_TEST_PASSWORD="$neo4j_second_password" \
BACKUP_NEO4J_TEST_URI="bolt://127.0.0.1:$backup_neo4j_port" \
BACKUP_NEO4J_TEST_PASSWORD="$backup_neo4j_password" \
BACKUP_NEO4J_PORT="$backup_neo4j_port" \
BACKUP_TENANT_COMPOSE_FILE="$backup_source_compose" \
BACKUP_TENANT_SECRETS_ROOT="$backup_tenant_secrets_root" \
BACKUP_CONTROL_PASSWORD_FILE="$secret_directory/postgres_password" \
BACKUP_AGE_IDENTITY_FILE="$secret_directory/backup-age-identity" \
BACKUP_AGE_RECIPIENTS_FILE="$secret_directory/backup-age-recipients" \
BACKUP_WRONG_AGE_IDENTITY_FILE="$secret_directory/backup-wrong-age-identity" \
BACKUP_EMBEDDING_VECTOR_FILE="$secret_directory/backup-embedding-vector.json" \
PLATFORM_TEST_IMAGE="$platform_image" \
python3.12 -m pytest tests/integration -q --ignore=tests/integration/test_release_fault_recovery.py
