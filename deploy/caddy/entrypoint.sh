#!/bin/sh
set -eu

token_file="${CLOUDFLARE_API_TOKEN_FILE:-/run/secrets/cloudflare_api_token}"
host_file="${MEMORY_PUBLIC_HOST_FILE:-/run/secrets/memory_public_host}"

if [ ! -r "$token_file" ]; then
    echo "Cloudflare API token file is not readable" >&2
    exit 1
fi

CLOUDFLARE_API_TOKEN="$(tr -d '\r\n' < "$token_file")"
if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
    echo "Cloudflare API token file is empty" >&2
    exit 1
fi
export CLOUDFLARE_API_TOKEN

if [ ! -r "$host_file" ]; then
    echo "Public hostname file is not readable" >&2
    exit 1
fi

MEMORY_PUBLIC_HOST="$(tr -d '\r\n' < "$host_file")"
if [ -z "$MEMORY_PUBLIC_HOST" ]; then
    echo "Public hostname file is empty" >&2
    exit 1
fi
if [ "${#MEMORY_PUBLIC_HOST}" -gt 253 ] \
    || ! printf '%s' "$MEMORY_PUBLIC_HOST" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' \
    || printf '%s' "$MEMORY_PUBLIC_HOST" | grep -q '\.\.'; then
    echo "Public hostname file is invalid" >&2
    exit 1
fi
export MEMORY_PUBLIC_HOST

exec caddy "$@"
