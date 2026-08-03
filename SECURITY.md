# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use
[GitHub's private vulnerability reporting](https://github.com/0xiDen/coengram/security/advisories/new)
and include:

- the affected commit, release, component, and deployment mode;
- a minimal reproduction that contains no real credentials or memory content;
- the expected impact across Principal, Tenant, storage, and operator boundaries;
- any known mitigation or evidence of active exploitation.

Please allow time to reproduce, scope, and coordinate a fix before public disclosure.

## Supported versions

Until CoEngram reaches a stable release, security fixes target the latest published
minor release and the default branch. Operators must deploy image digests—not mutable
tags alone—and keep recovery points current before upgrading.

## Security boundaries

CoEngram is designed to protect application-level identity, memory scope, Tenant
routing, durable publication, and content-safe telemetry. Operators remain responsible
for the host, Docker daemon, firewall, DNS and Cloudflare account, TLS policy, SSH,
disk encryption, provider accounts, backup replication, and access to secret files.

Compromise of the Docker host or operator credentials is outside the application's
isolation boundary. Iteration 1 is single-server and does not claim high availability.

The gateway and worker are shared, trusted control-plane components. They resolve a
server-derived Tenant route and mount the protected per-Tenant credentials needed to
open that route. Compromise of either application container can therefore cross Tenant
storage boundaries even though ordinary requests, database roles, Neo4j instances,
and volumes are isolated. Treat application-image provenance, host access, dependency
updates, and secret-file permissions as controls for the whole deployment—not for only
one Tenant. Strong infrastructure isolation of the shared control plane is a future
hardening step, not an iteration-1 guarantee.

Production knowledge synthesis sends only the explicitly selected Private Memory and
published Tenant Knowledge required for that run to the configured Anthropic model
API. Those selected contents leave the self-hosted memory boundary and are governed by
the operator's provider account, region, retention, and data-use terms. Operators that
cannot permit this egress must leave live model execution disabled or implement and
review a self-hosted provider adapter before enabling synthesis.
