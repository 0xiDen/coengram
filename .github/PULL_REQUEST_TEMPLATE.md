## Outcome

Describe the externally visible behavior and why it is needed.

## Boundary review

- [ ] Tenant and Principal scope still come only from authenticated server state.
- [ ] Private content cannot enter Tenant Knowledge without human review.
- [ ] Failure, retry, idempotency, migration, and recovery behavior are documented.
- [ ] Logs, metrics, traces, fixtures, and screenshots contain no sensitive content.
- [ ] No real credentials, deployment hostnames, or private identifiers are included.

## Verification

- [ ] `make verify`
- [ ] `make package`
- [ ] Public contracts and operator docs are updated.
