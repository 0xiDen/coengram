#!/bin/sh
set -eu

image_id="$(docker build --quiet -f deploy/caddy/Dockerfile deploy/caddy)"
run_id="$$"
network="memory-caddy-verify-$run_id"
gateway_container="memory-caddy-gateway-$run_id"
caddy_container="memory-caddy-ingress-$run_id"
secret_directory="$(mktemp -d)"

cleanup() {
  docker rm -f "$caddy_container" "$gateway_container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$secret_directory"
}
trap cleanup EXIT INT TERM

printf '%s\n' '0123456789abcdef0123456789abcdef01234567' \
  > "$secret_directory/cloudflare_api_token"
printf '%s\n' 'memory.example.com' > "$secret_directory/memory_public_host"
chmod 0600 "$secret_directory/cloudflare_api_token" "$secret_directory/memory_public_host"

docker run --rm --entrypoint caddy "$image_id" list-modules \
  | grep -Fx 'dns.providers.cloudflare' >/dev/null

docker run --rm \
  --volume "$secret_directory/cloudflare_api_token:/run/secrets/cloudflare_api_token:ro" \
  --volume "$secret_directory/memory_public_host:/run/secrets/memory_public_host:ro" \
  --volume "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  "$image_id" validate --config /etc/caddy/Caddyfile --adapter caddyfile

printf '%s\n' 'https://memory.example.com' > "$secret_directory/memory_public_host"
if docker run --rm \
  --volume "$secret_directory/cloudflare_api_token:/run/secrets/cloudflare_api_token:ro" \
  --volume "$secret_directory/memory_public_host:/run/secrets/memory_public_host:ro" \
  --volume "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  "$image_id" validate --config /etc/caddy/Caddyfile --adapter caddyfile \
  >/dev/null 2>&1; then
  printf '%s\n' "Caddy accepted an invalid secret-supplied hostname" >&2
  exit 1
fi
printf '%s\n' 'memory.example.com' > "$secret_directory/memory_public_host"

docker network create "$network" >/dev/null
docker run --detach --name "$gateway_container" --network "$network" \
  --network-alias gateway \
  --volume "$PWD/scripts/fake-ingress-gateway.py:/test/fake-ingress-gateway.py:ro" \
  python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df \
  python /test/fake-ingress-gateway.py >/dev/null
docker run --detach --name "$caddy_container" --network "$network" \
  --publish 127.0.0.1::8080 \
  --entrypoint caddy \
  "$image_id" run --config /etc/caddy/Caddyfile.test --adapter caddyfile >/dev/null

caddy_port="$(docker port "$caddy_container" 8080/tcp | sed -n '1s/.*://p')"
attempt=0
while :; do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:$caddy_port/api/v1/memories" || true)"
  [ "$status" = "401" ] && break
  attempt=$((attempt + 1))
  [ "$attempt" -lt 30 ] || {
    printf '%s\n' "Caddy black-box ingress did not become ready" >&2
    exit 1
  }
  sleep 1
done
printf '%s\n' "PASS unauthenticated HTTP rejected"

curl --fail --silent --show-error \
  --header 'Authorization: Bearer release-verification-token' \
  --header 'X-Tenant-ID: caller-controlled' \
  --header 'X-Principal-ID: caller-controlled' \
  --header 'X-Subject-User-ID: caller-controlled' \
  --header 'X-Actor-ID: caller-controlled' \
  "http://127.0.0.1:$caddy_port/api/v1/memories" \
  | grep -F '"surface":"http"' >/dev/null
printf '%s\n' "PASS authenticated HTTP forwarded with authoritative headers stripped"

curl --fail --silent --show-error \
  --request POST \
  --header 'Authorization: Bearer release-verification-token' \
  "http://127.0.0.1:$caddy_port/mcp" \
  | grep -F '"surface":"mcp"' >/dev/null
printf '%s\n' "PASS authenticated MCP forwarded"

curl --fail --silent --show-error \
  --header 'X-Tenant-ID: caller-controlled' \
  --header 'X-Principal-ID: caller-controlled' \
  "http://127.0.0.1:$caddy_port/.well-known/oauth-protected-resource" \
  | grep -F '"surface":"oauth-protected-resource"' >/dev/null
printf '%s\n' "PASS OAuth protected-resource metadata forwarded with authoritative headers stripped"

dd if=/dev/zero of="$secret_directory/oversized-telegram-body" bs=1024 count=257 \
  >/dev/null 2>&1
status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --request POST \
  --header 'Authorization: Bearer release-verification-token' \
  --header 'Content-Type: application/json' \
  --data-binary "@$secret_directory/oversized-telegram-body" \
  "http://127.0.0.1:$caddy_port/api/v1/telegram/webhook")"
[ "$status" = "413" ] || {
  printf '%s\n' "Oversized Telegram request returned $status instead of 413" >&2
  exit 1
}
printf '%s\n' "PASS oversized public request rejected with 413"

private_test_host='operator-secret.example.invalid'
curl --fail --silent --show-error \
  --header "Host: $private_test_host" \
  --header 'Authorization: Bearer release-verification-token' \
  "http://127.0.0.1:$caddy_port/api/v1/memories" >/dev/null
sleep 1
if docker logs "$caddy_container" 2>&1 | grep -F "$private_test_host" >/dev/null; then
  printf '%s\n' "Secret hostname appeared in Caddy telemetry" >&2
  exit 1
fi
printf '%s\n' "PASS secret hostname excluded from Caddy telemetry"

for path in neo4j rabbitmq grafana metrics admin; do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header 'Authorization: Bearer release-verification-token' \
    "http://127.0.0.1:$caddy_port/$path")"
  [ "$status" = "404" ] || {
    printf '%s\n' "Unexpected infrastructure route: $path returned $status" >&2
    exit 1
  }
done
printf '%s\n' "PASS infrastructure route probes are not public"
