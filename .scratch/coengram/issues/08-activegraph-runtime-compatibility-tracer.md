# 08. ActiveGraph runtime compatibility tracer

Status: resolved

Blocked by: 03. Principal scopes, Delegation, and RBAC.

## What to build

Prove and encapsulate the evolving ActiveGraph dependency before the production Agent
is built. A deep Agent Runtime Module constructs tenant-scoped runtimes with event
storage, Frame, Budget, exact model recording, tools, telemetry, replay policy, and
versioned Pack loading; a deterministic tracer verifies persistence and replay through
public ActiveGraph behavior.

## Acceptance criteria

- [x] A proven ActiveGraph package version and compatible Python version are pinned with
      the reason and validation evidence documented.
- [x] The Agent Runtime Module exposes one construction Interface and keeps framework,
      store, provider, telemetry, and Pack wiring local to its Implementation.
- [x] Every runtime is bound to one Tenant Operations Store through server-derived
      Tenant Session routing and cannot select or fall back to another Tenant's events.
- [x] A versioned native ActiveGraph Pack loads into a fresh Runtime without global side
      effects and emits typed events through documented public APIs.
- [x] Agent Run creation accepts a registered capability and typed input rather than an
      arbitrary caller-supplied Pack name.
- [x] Durable Agent Run events reconstruct status and support deterministic replay from
      the Tenant Operations Store independently of Neo4j memory.
- [x] The runtime records exact provider/model identity and all material settings on
      every Agent Run.
- [x] Default budgets enforce three model calls, ten tool calls, one hundred events, two
      minutes, and USD 0.25; exhaustion records a budget event and resumable failed
      state.
- [x] Cancellation produces a durable terminal or resumable cancellation state and
      prevents additional model/tool work.
- [x] A deterministic fake or recorded model provider produces stable runs with no
      network access or billing.
- [x] Replay tests prove equivalent public run state and outputs without re-executing
      external side effects.
- [x] Compatibility tests use public runtime inspection, events, and state rather than
      asserting framework-private objects.

## Comments

_No comments yet._
