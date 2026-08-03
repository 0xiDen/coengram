# 09. Knowledge Synthesis Agent

Status: resolved

Blocked by: 04. Reviewed knowledge publication; 08. ActiveGraph runtime compatibility
tracer.

## What to build

Build the first production ActiveGraph Pack and generalized Agent Invocation path. A
User starts `knowledge_synthesis` with an explicit request and selected Private Memory
references, receives an Agent Run identifier, and can inspect or cancel the run. The
Agent recalls Tenant Knowledge, detects duplicates and conflicts, and submits a safe
Knowledge Candidate for human review without ever approving it.

## Acceptance criteria

- [x] A versioned native `knowledge_synthesis` ActiveGraph Pack declares typed input,
      events, behaviors, tools, prompts, policies, settings, and output without global
      runtime setup.
- [x] `start_agent_run(capability, input)` accepts only registered capability schemas,
      applies idempotency, and returns an Agent Run identifier asynchronously.
- [x] Invocation requires explicitly selected Private Memory references authorized for
      the Tenant Session; the Agent cannot silently scan the User's full private history.
- [x] The Pack recalls published Tenant Knowledge and records duplicate and conflict
      findings used to formulate the candidate.
- [x] The resulting Knowledge Candidate contains a distilled claim, confidence, safe
      provenance, and duplicate/conflict information without exposing source content to
      reviewers.
- [x] The Agent can propose but cannot approve, reject, administer, or bypass Promotion.
- [x] Agent status and cancellation are available through typed Agent Invocation
      operations, and durable events make failure and resumption explicit.
- [x] User-derived content is not copied into the Agent's own Private Memory; reusable
      Agent operating lessons can be retained only under the Agent's ownership and
      selective-retention rules.
- [x] The deterministic provider is the default for automated tests and yields
      replayable, stable events and candidates.
- [x] The production LangChain Anthropic Adapter uses exact model
      `claude-sonnet-5`, records its model ID and material settings, parses structured
      output, maps provider failures, and accounts for budgets.
- [x] Default tests require no live billing; an explicitly enabled smoke check may use
      an operator-supplied file-backed Anthropic secret.
- [x] Tests cover idempotent start, authorization, selected-reference isolation,
      duplication, conflict, budgets, cancellation, replay, provider failure, candidate
      submission, and the Agent's inability to approve.

## Comments

_No comments yet._
