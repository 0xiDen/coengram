"""Versioned, secret-free declarative Tenant Manifests."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_memory_service.models import PrincipalKind
from agent_memory_service.roles import VALID_ROLES

_SAFE_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
_POSTGRES_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
_SERVICE_NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {
        "access_token",
        "anthropic_api_key",
        "api_key",
        "cloudflare_api_token",
        "credential",
        "credentials",
        "neo4j_password",
        "password",
        "postgres_password",
        "secret",
        "secrets",
        "telegram_bot_token",
        "token_verifier",
    }
)


class ManifestPrincipal(BaseModel):
    """One User or Agent declared inside a Tenant Manifest."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    principal_id: str = Field(min_length=3, max_length=63, pattern=_SAFE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    kind: PrincipalKind


class ManifestMembership(BaseModel):
    """Roles granted to one declared Principal in the manifest's Tenant."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    principal_id: str = Field(min_length=3, max_length=63, pattern=_SAFE_ID_PATTERN)
    roles: tuple[str, ...] = Field(min_length=1, max_length=len(VALID_ROLES))

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, roles: tuple[str, ...]) -> tuple[str, ...]:
        unique = tuple(sorted(set(roles)))
        if len(unique) != len(roles):
            raise ValueError("Tenant Membership roles must not contain duplicates")
        unknown = set(unique).difference(VALID_ROLES)
        if unknown:
            raise ValueError("Tenant Membership contains an unknown role")
        return unique


class TenantPolicies(BaseModel):
    """Iteration-1 Tenant policy values bounded by platform maxima."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_token_lifetime_days: int = Field(default=90, ge=1, le=90)
    delegated_agent_token_lifetime_days: int = Field(default=90, ge=1, le=90)
    autonomous_agent_token_lifetime_days: int = Field(default=30, ge=1, le=30)
    automatic_private_retention: bool = True
    promotion_requires_human_review: Literal[True] = True
    erasure_requires_admin_approval: Literal[True] = True


class TenantManifest(BaseModel):
    """Canonical declarative input for one isolated Tenant.

    Infrastructure names are derived from the immutable Tenant identifier. A manifest
    may include those names for transparent round trips, but cannot redirect a Tenant
    to operator-selected database, role, or service names.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    version: Literal[1] = 1
    tenant_id: str = Field(min_length=3, max_length=40, pattern=_SAFE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    database_name: str = Field(
        default="",
        min_length=3,
        max_length=63,
        pattern=_POSTGRES_NAME_PATTERN,
    )
    database_role: str = Field(
        default="",
        min_length=3,
        max_length=63,
        pattern=_POSTGRES_NAME_PATTERN,
    )
    neo4j_service_name: str = Field(
        default="",
        min_length=3,
        max_length=63,
        pattern=_SERVICE_NAME_PATTERN,
    )
    principals: tuple[ManifestPrincipal, ...] = ()
    memberships: tuple[ManifestMembership, ...] = ()
    policies: TenantPolicies = Field(default_factory=TenantPolicies)

    @model_validator(mode="before")
    @classmethod
    def derive_infrastructure_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        tenant_id = value.get("tenant_id")
        if not isinstance(tenant_id, str):
            return value
        normalized = tenant_id.replace("-", "_")
        populated = dict(value)
        populated.setdefault("database_name", f"tenant_{normalized}")
        populated.setdefault("database_role", f"tenant_{normalized}_rw")
        populated.setdefault("neo4j_service_name", f"neo4j-{tenant_id}")
        return populated

    @model_validator(mode="after")
    def validate_manifest_links_and_names(self) -> Self:
        normalized = self.tenant_id.replace("-", "_")
        expected_names = {
            "database_name": f"tenant_{normalized}",
            "database_role": f"tenant_{normalized}_rw",
            "neo4j_service_name": f"neo4j-{self.tenant_id}",
        }
        for field_name, expected in expected_names.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must be derived from the immutable Tenant ID")

        principal_by_id = {principal.principal_id: principal for principal in self.principals}
        if len(principal_by_id) != len(self.principals):
            raise ValueError("Tenant Manifest Principal identifiers must be unique")

        memberships_by_principal = {
            membership.principal_id: membership for membership in self.memberships
        }
        if len(memberships_by_principal) != len(self.memberships):
            raise ValueError("Tenant Manifest has duplicate Tenant Memberships")
        if set(memberships_by_principal).difference(principal_by_id):
            raise ValueError("Tenant Membership refers to an undeclared Principal")
        for membership in self.memberships:
            principal = principal_by_id[membership.principal_id]
            if principal.kind is PrincipalKind.AGENT and any(
                role != "tenant_member" for role in membership.roles
            ):
                raise ValueError("Agents cannot receive administrative or Curator roles")
        return self

    def export_json(self) -> str:
        """Return a stable, secret-free JSON representation suitable for versioning."""
        document = self.model_dump(mode="json")
        _reject_secret_fields(document)
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    @classmethod
    def import_json(cls, document: str) -> Self:
        """Parse and validate an exported or operator-authored manifest."""
        try:
            value = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ValueError("Tenant Manifest is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Tenant Manifest must be a JSON object")
        _reject_secret_fields(value)
        return cls.model_validate(value)

    @property
    def fingerprint(self) -> str:
        """Stable content identity used to reject unsafe resume with changed input."""
        return hashlib.sha256(self.export_json().encode("utf-8")).hexdigest()


def _reject_secret_fields(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_SECRET_FIELDS:
                field = ".".join((*path, str(key)))
                raise ValueError(f"Tenant Manifest cannot contain secret field: {field}")
            _reject_secret_fields(child, (*path, str(key)))
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _reject_secret_fields(child, (*path, str(index)))
