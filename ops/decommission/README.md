# Tenant decommission operations boundary

`request.example.json` shows the immutable identifiers that an operator must
resolve and review before opening a request. Never infer destructive targets from
a substring, wildcard, Docker label shared by multiple tenants, or an environment
variable that has not been resolved and checked.

The only permitted workflow is the `DecommissionService` state machine:

`suspending -> suspended -> grace-period -> finalizing -> tombstone`

Cancellation is possible only from `grace-period` before its deadline. A request
and its confirmation are separate calls by different operators. Finalization is
not permitted until 30 complete days after confirmation.

Production adapters implement the six exact, idempotent methods on
`TenantDestructionPort`. Each call receives a `TenantResourceIdentity`; adapters
must verify that identity against the control store before removing anything.
There is deliberately no generic "prune", shared-volume deletion, shared Compose
shutdown, or shared PostgreSQL operation in this interface.

The recovery backup/export is not one of the deletion targets. Its external
retention policy continues independently after the tenant tombstone is written.
See `docs/runbooks/decommission.md` for the review, cancellation, retry, and audit
procedure.
