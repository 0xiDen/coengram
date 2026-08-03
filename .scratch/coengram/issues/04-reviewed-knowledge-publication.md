# 04. Reviewed knowledge publication

Status: resolved

Blocked by: 03. Principal scopes, Delegation, and RBAC.

## What to build

Create the deliberate path from Private Memory to trusted Tenant Knowledge. A Principal
submits a distilled Knowledge Candidate with safe provenance; a human Curator reviews
it; PostgreSQL commits the decision and outbox atomically; RabbitMQ and an idempotent
worker publish approved knowledge to the correct graph before recall can observe it.

## Acceptance criteria

- [x] A Principal can propose a Knowledge Candidate from explicitly selected owned or
      delegated Private Memory references without changing source visibility.
- [x] A candidate records the distilled claim, confidence, proposer, safe provenance,
      and duplicate or conflict information without exposing source Private Memory to
      reviewers.
- [x] Tenant Members and Agents may propose candidates; only a human Knowledge Curator
      or Tenant Administrator may approve or reject them.
- [x] A Tenant Administrator may self-approve only when the Tenant has exactly one human
      member; Agents can never approve or administer.
- [x] Promotion has explicit draft, review, publishing, published, rejected, and failure
      behavior with forbidden transitions rejected and audited.
- [x] PostgreSQL atomically commits state-changing commands, governance audit, Promotion
      state, idempotency result, and transactional outbox record.
- [x] The outbox relay publishes durable RabbitMQ messages and a worker acknowledges
      only after idempotently applying the event to the server-routed Tenant Memory
      Store.
- [x] Duplicate delivery and worker crash before acknowledgement do not create duplicate
      Tenant Knowledge.
- [x] Repeated commands with the same idempotency key return the original operation
      rather than creating duplicate candidates, reviews, or publications.
- [x] Only `published` Tenant Knowledge appears in ordinary recall; draft, rejected,
      publishing, and failed candidates remain excluded.
- [x] Broker or graph outages leave authoritative, visible, retryable PostgreSQL state;
      poison messages become inspectable dead letters without losing source truth.
- [x] Real PostgreSQL, RabbitMQ, and separate Neo4j Community integration tests cover
      atomicity, outage recovery, redelivery, idempotency, retry, dead-letter behavior,
      routing isolation, and published-only visibility.

## Comments

_No comments yet._
