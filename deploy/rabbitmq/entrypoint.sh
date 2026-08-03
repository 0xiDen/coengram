#!/usr/bin/env bash
set -euo pipefail

password_file="${MEMORY_RABBITMQ_SEED_PASSWORD_FILE:-/run/secrets/rabbitmq_password}"
seed_user="${MEMORY_RABBITMQ_SEED_USER:-memory}"

if [[ ! -r "$password_file" ]]; then
    echo "RabbitMQ seed password file is not readable" >&2
    exit 1
fi

seed_password="$(tr -d '\r\n' < "$password_file")"
if [[ -z "$seed_password" ]]; then
    echo "RabbitMQ seed password file is empty" >&2
    exit 1
fi

# RabbitMQ interpolates these values into single-quoted configuration entries.
# Requiring a base64url-compatible alphabet avoids configuration injection.
if [[ ! "$seed_user" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "RabbitMQ seed user must use only letters, digits, underscore, or hyphen" >&2
    exit 1
fi
if [[ ! "$seed_password" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "RabbitMQ seed password must be high-entropy base64url without padding" >&2
    exit 1
fi

export MEMORY_RABBITMQ_SEED_PASSWORD="$seed_password"

exec /usr/local/bin/docker-entrypoint.sh "$@"
