# 12. Central observability and shadow limits

Status: resolved

Blocked by: 04. Reviewed knowledge publication; 08. ActiveGraph runtime compatibility
tracer; 11. Production ingress and Compose topology.

## What to build

Give operators one content-safe operational view across Caddy, the gateway, memory
workers, PostgreSQL, RabbitMQ, Neo4j Tenant services, ActiveGraph, LangChain, and
Telegram. Alloy collects correlated metrics, logs, and traces into Mimir, Loki, and
Tempo; Grafana provisions operator-only data sources and dashboards. Iteration-1 quota
thresholds emit would-have-limited telemetry and never reject with `429`.

## Acceptance criteria

- [x] Grafana Alloy collects logs, metrics, and traces from every shared service and
      per-Tenant component named in the PRD through private endpoints.
- [x] Mimir retains metrics for 30 days, Loki retains logs for 14 days, and Tempo retains
      traces for seven days with storage and lifecycle settings documented.
- [x] Grafana starts with provisioned Mimir, Loki, and Tempo data sources and
      operator-only authentication; Tenant users receive no Grafana access.
- [x] Provisioned dashboards cover authentication, request outcomes, latency, Tenant
      routes, Agent Runs and budgets, outbox lag, RabbitMQ delivery/dead letters,
      workers, PostgreSQL, Neo4j, Caddy, Telegram, and dependency availability.
- [x] Metrics, logs, and traces correlate using opaque Tenant, Principal, Agent Run,
      operation, and request/correlation identifiers.
- [x] Telemetry contains no Access Token or verifier, prompt, Memory Item content,
      Knowledge Candidate content, model input/output, Telegram message text, database
      credential, or raw external identity.
- [x] Detailed Agent Run audit remains in the Tenant Operations Store; Tempo captures
      execution shape and timing only.
- [x] Shadow thresholds are measured for 120 requests/minute per token, 600 per Tenant,
      ten concurrent embedding/search operations, and two concurrent synthesis runs.
- [x] Requests exceeding a shadow threshold continue normally and emit utilization plus
      would-have-limited metrics/logs; iteration 1 never returns `429` because of these
      thresholds.
- [x] Unused-token signals and shadow utilization can be queried and visualized without
      exposing a working credential or domain content.
- [x] Observability tests exercise representative success, denial, failure, retry,
      publication, and Agent Run paths and assert expected correlations.
- [x] Automated leakage tests scan captured logs and traces for seeded forbidden
      secrets and domain content and fail if any are present.

## Comments

_No comments yet._
