# 10. Telegram invocation Adapter

Status: resolved

Blocked by: 07. MCP and Claude Code integration; 09. Knowledge Synthesis Agent.

## What to build

Adapt Telegram direct messages into the same authenticated memory commands and Agent
Invocations. Operators bind one numeric Telegram identity to one User, Tenant
Membership, and Agent Delegation; command-oriented interactions are explicit and
auditable, while usernames, message text, group traffic, and unbound senders cannot
choose or mutate security context.

## Acceptance criteria

- [x] The operator CLI creates, inspects, disables, and removes a Channel Binding from
      one numeric Telegram identity to one User, one Tenant Membership, and one Agent
      Delegation.
- [x] Iteration 1 permits exactly one active Tenant binding per Telegram identity and
      rejects ambiguous or duplicate bindings explicitly.
- [x] The webhook validates Telegram's configured secret token before parsing or
      invoking any command.
- [x] Only direct-message updates are accepted; group, channel, edited, malformed, and
      unsupported updates cause no memory or Agent Run mutation.
- [x] `/whoami`, `/recall`, `/remember`, `/propose`, `/synthesize`, `/status`, and
      `/cancel` adapt to canonical authenticated Interfaces with typed inputs and
      idempotency. `/synthesize` starts a Knowledge Synthesis Agent Run from explicitly
      selected Private Memory references and returns its durable identifier.
- [x] Numeric sender identity resolves the Channel Binding; Telegram username, headers,
      chat text, and command arguments cannot select Tenant, User, Agent, or Subject
      User.
- [x] The Telegram integration acts as the Agent Actor and the mapped User remains the
      delegated Subject User in audit and memory scope.
- [x] Ordinary free-form text returns command guidance and creates no Memory Item,
      Knowledge Candidate, or Agent Run.
- [x] Agent Invocation reply context carries only the information needed to deliver an
      asynchronous outcome without coupling Telegram details to core Agent behavior.
- [x] Replies do not include Access Tokens, internal routes, database/graph identifiers,
      private source content, or another Tenant's existence.
- [x] Tests use representative webhook fixtures and a fake outbound Telegram Adapter;
      they require no live Telegram API.
- [x] Tests cover webhook authentication, direct-message restriction, binding lookup,
      command parsing, idempotency, audit attribution, reply context, unbound users, and
      non-mutation for ordinary text.

## Comments

_No comments yet._
