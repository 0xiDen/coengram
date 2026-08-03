# 11. Production ingress and Compose topology

Status: resolved

Blocked by: 02. Declarative Tenant lifecycle and hard isolation; 07. MCP and Claude Code
integration.

## What to build

Package the full platform for a single production server. Custom Caddy terminates TLS
for a hostname supplied through a protected secret file using the Cloudflare DNS provider and proxies only to the
authenticated gateway. Shared services and per-Tenant Neo4j Community projects use
file-backed secrets, private networks, unique routes, and no public infrastructure
ports.

## Acceptance criteria

- [x] The root Compose model runs the complete local and one-Tenant integration
      environment with deterministic health checks and startup dependencies.
- [x] A shared production Compose model includes Caddy, authenticated gateway, workers,
      shared PostgreSQL, RabbitMQ, Alloy, Mimir, Loki, Tempo, and Grafana.
- [x] A parameterized per-Tenant Compose model creates one Neo4j Community service,
      credential set, and persistent volume per immutable Tenant identifier.
- [x] Tenant services join a private shared network under unique generated addresses;
      routing comes only from activated Control Store records.
- [x] PostgreSQL, RabbitMQ, Neo4j Bolt/Browser, and observability endpoints publish no
      public host ports in the production topology.
- [x] The custom Caddy image is built with the official Cloudflare DNS provider plugin,
      and module-list validation proves the plugin is present.
- [x] Caddy serves the secret-supplied hostname, obtains DNS-01 certificates from a
      file-backed Cloudflare token, exposes its admin endpoint only internally, and
      emits JSON access logs and metrics.
- [x] Caddy routes `/mcp` and versioned typed HTTP only to the authenticated gateway;
      storage and observability services have no public routes.
- [x] Generated database, broker, graph, and application credentials live in protected
      secret files mounted under the container secret path; non-secret environment
      files contain no credentials.
- [x] Operator-supplied Cloudflare, Anthropic, Telegram, and backup credentials use the
      same file-backed secret Adapter and are absent from images, Compose rendering,
      logs, and source control.
- [x] Secret rotation procedures cover Access Tokens and service credentials with
      overlap or controlled restart appropriate to each credential.
- [x] Caddy configuration and black-box ingress tests prove unauthenticated requests are
      rejected and internal services are not publicly routable.
- [x] Documentation clearly leaves Cloudflare record creation/proxy mode, firewalling,
      host disk encryption, SSH setup, and remote backup credentialing outside the repo.

## Comments

_No comments yet._
