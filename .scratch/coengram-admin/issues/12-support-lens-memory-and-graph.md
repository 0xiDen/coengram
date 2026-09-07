# 12. Support Lens Memory and Graph

Status: implemented locally

## Goal

Give Operators a non-impersonating Support Lens for tenant memory diagnostics: Private
Memory metadata for a selected Principal, published Tenant Knowledge, and a lightweight
knowledge graph visualization.

## Acceptance

- [x] Admin API lists content-free Private Memory metadata for a selected Tenant and
      Principal.
- [x] Admin API lists published Tenant Knowledge for a selected Tenant.
- [x] Admin API returns a graph document linking Tenants, Principals, Knowledge
      Candidates, reviewers, and published Tenant Knowledge without source Private Memory
      content.
- [x] Support Lens reads are recorded as content-safe Operator Audit Events.
- [x] Chakra admin frontend includes a Memory page with private metadata, tenant
      knowledge, and graph visualization.
- [x] Local tests cover response shape, graph links, and privacy-safe audit behavior.
