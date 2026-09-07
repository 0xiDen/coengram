# 11. Admin Knowledge Candidate Review

Status: implemented locally

## Goal

Let `knowledge_admin` and `operator_admin` Operators inspect and review Tenant Knowledge
Candidates from the admin panel without exposing Private Memory source content or granting
the Operator a Tenant Session.

## Acceptance

- [x] Admin API lists privacy-safe Knowledge Candidate views for a selected Tenant.
- [x] Admin API approves or rejects candidates with CSRF protection and role gating.
- [x] Operator Audit Events record review target IDs and decision/status metadata only.
- [x] Chakra admin frontend includes a Knowledge page with tenant filtering and review
      actions.
- [x] Local tests cover happy path, audit safety, and role rejection.
