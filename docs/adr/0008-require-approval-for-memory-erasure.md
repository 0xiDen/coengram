# Require Tenant Administrator approval for memory erasure

Iteration 1 allows Principals to inspect, export, and supersede their Private Memory but not delete it directly. Erasure requires an audited request and human Tenant Administrator approval, after which content is removed from storage and recall while a content-free tombstone preserves accountability. Completion also scrubs the free-text request reason and review rationale from both request and review tables; only identifiers, actors, the approval decision, and timing remain portable.
