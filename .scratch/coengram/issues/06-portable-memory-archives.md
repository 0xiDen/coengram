# 06. Portable memory archives

Status: resolved

Blocked by: 05. Correction and approved erasure.

## What to build

Make memory portable without weakening privacy or governance. Principals export and
import their own Private Memory, while Tenant Administrators export Tenant Knowledge
and governance history. Versioned, checksummed JSON Lines preserves identity,
provenance, corrections, and tombstones; importing shared material creates candidates
rather than trusted knowledge.

## Acceptance criteria

- [x] A documented, versioned JSON Lines Memory Archive format includes stable
      identifiers, scope metadata, provenance, supersedence, content-free tombstones,
      record checksums, and an archive checksum.
- [x] A Principal can export only its owned Private Memory, including safe history
      needed to preserve correction and erasure semantics.
- [x] A delegated Agent cannot export the Subject User's Private Memory as its own or
      include user-derived content in an Agent-owned archive.
- [x] A Tenant Administrator can export published Tenant Knowledge and governance
      history but cannot include any Principal's Private Memory.
- [x] Exports contain no working Access Tokens, token verifiers, database credentials,
      generated secrets, prompts, model input/output, or Agent Records.
- [x] Import validates format version, checksums, stable identifiers, scope, and
      provenance before accepting any records.
- [x] A private archive imports only into the authenticated owner's Private Memory
      scope and never changes content visibility.
- [x] Imported shared material becomes reviewable Knowledge Candidates and cannot enter
      published Tenant Knowledge directly.
- [x] Import is idempotent and reports stable original results for repeated archives;
      conflicting identifiers fail explicitly without partial cross-scope writes.
- [x] Canonical fixed-checksum fixtures round-trip Private Memory, corrections,
      tombstones, Tenant Knowledge, and governance history through public Interfaces.
- [x] Isolation and negative tests prove that crafted archives cannot select another
      Tenant, owner, Subject User, graph, or database.

## Comments

_No comments yet._
