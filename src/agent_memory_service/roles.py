"""Shared Control Store role constants."""

from __future__ import annotations

VALID_ROLES = frozenset({"tenant_administrator", "knowledge_curator", "tenant_member"})
VALID_OPERATOR_ROLES = frozenset(
    {
        "operator_admin",
        "identity_admin",
        "tenant_provisioner",
        "tenant_support",
        "knowledge_admin",
        "token_admin",
        "audit_viewer",
    }
)
