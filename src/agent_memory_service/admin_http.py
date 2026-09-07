"""HTTP Adapter for Operator admin routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from agent_memory_service.auth import AuthenticationError, RotatedCredential, TokenRecord
from agent_memory_service.control import (
    ControlConflict,
    ControlModule,
    ControlNotFound,
    MembershipRecord,
    OperatorRecord,
    PrincipalRecord,
)
from agent_memory_service.manifest import TenantManifest
from agent_memory_service.operator_audit import OperatorAuditEventView, operator_audit_event_view
from agent_memory_service.operator_auth import (
    IssuedAdminSession,
    OperatorSession,
    OperatorTokenRecord,
)
from agent_memory_service.operator_provisioning import (
    ProvisioningJobView,
    provisioning_job_view,
)

ADMIN_SESSION_COOKIE = "coengram_admin_session"
ADMIN_CSRF_HEADER = "X-CoEngram-CSRF"
ADMIN_COOKIE_MAX_AGE_SECONDS = 8 * 60 * 60
TENANT_VISIBLE_ROLES = frozenset(
    {"tenant_provisioner", "tenant_support", "identity_admin", "audit_viewer", "operator_admin"}
)
IDENTITY_MUTATION_ROLES = frozenset({"identity_admin", "operator_admin"})
AUDIT_VISIBLE_ROLES = frozenset({"audit_viewer", "operator_admin"})
PROVISIONING_MUTATION_ROLES = frozenset({"tenant_provisioner", "operator_admin"})
PROVISIONING_VISIBLE_ROLES = frozenset(
    {"tenant_provisioner", "tenant_support", "audit_viewer", "operator_admin"}
)
OPERATOR_ADMIN_ROLES = frozenset({"operator_admin"})
IDENTITY_VISIBLE_ROLES = frozenset(
    {"identity_admin", "tenant_support", "token_admin", "audit_viewer", "operator_admin"}
)
TOKEN_VISIBLE_ROLES = frozenset({"token_admin", "audit_viewer", "operator_admin"})
TOKEN_MUTATION_ROLES = frozenset({"token_admin", "operator_admin"})


class AdminLoginBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    access_token: str = Field(min_length=1)


class AdminSessionCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    operator_id: str
    roles: tuple[str, ...]
    csrf_token: str
    absolute_expires_at: str
    idle_expires_at: str


class AdminSessionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    operator_id: str
    roles: tuple[str, ...]


class TenantView(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    name: str
    active: bool


class TenantListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenants: tuple[TenantView, ...]


class OperatorView(BaseModel):
    model_config = ConfigDict(frozen=True)

    operator_id: str
    name: str
    roles: tuple[str, ...]
    active: bool


class OperatorListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    operators: tuple[OperatorView, ...]


class CreateOperatorBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    operator_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    roles: tuple[str, ...] = Field(min_length=1)


class UpdateOperatorBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    roles: tuple[str, ...] | None = Field(default=None, min_length=1)
    active: bool | None = None


class PrincipalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal_id: str
    name: str
    kind: str
    active: bool


class MembershipView(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    principal_id: str
    roles: tuple[str, ...]
    active: bool


class PrincipalListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    principals: tuple[PrincipalView, ...]
    memberships: tuple[MembershipView, ...]


class CredentialView(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    access_token: str
    expires_at: str
    warning: str


class TokenRecordView(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    tenant_id: str
    principal_id: str
    actor_kind: str
    roles: tuple[str, ...]
    subject_user_id: str | None
    delegation_id: str | None
    issued_at: str
    expires_at: str
    revoked_at: str | None
    last_used_at: str | None
    active: bool


class TokenListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    tokens: tuple[TokenRecordView, ...]


class OperatorTokenRecordView(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    operator_id: str
    roles: tuple[str, ...]
    issued_at: str
    expires_at: str
    revoked_at: str | None
    last_used_at: str | None
    active: bool


class OperatorTokenListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    tokens: tuple[OperatorTokenRecordView, ...]


class IssueOperatorTokenBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lifetime_days: int | None = Field(default=None, ge=1, le=30)


class IssueTokenBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    lifetime_days: int | None = Field(default=None, ge=1, le=90)


class RotateTokenBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlap_minutes: int = Field(ge=1, le=24 * 60)
    lifetime_days: int | None = Field(default=None, ge=1, le=90)


class RotateOperatorTokenBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlap_minutes: int = Field(ge=1, le=24 * 60)
    lifetime_days: int | None = Field(default=None, ge=1, le=30)


class RotatedCredentialView(BaseModel):
    model_config = ConfigDict(frozen=True)

    previous_token_id: str
    previous_valid_until: str
    credential: CredentialView


class CreateUserBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    roles: tuple[str, ...] = Field(min_length=1)
    issue_token: bool = False
    token_lifetime_days: int | None = Field(default=None, ge=1, le=90)


class CreatedUserView(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal: PrincipalView
    membership: MembershipView
    credential: CredentialView | None = None


class CreateProvisioningJobBody(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    manifest: TenantManifest


class ProvisioningJobListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    jobs: tuple[ProvisioningJobView, ...]


class OperatorAuditEventListView(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: tuple[OperatorAuditEventView, ...]


def mount_admin_routes(app: FastAPI, control: ControlModule) -> None:
    """Mount Operator-admin routes onto the shared gateway app."""

    @app.post(
        "/api/v1/admin/session",
        response_model=AdminSessionCreated,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_admin_session(body: AdminLoginBody, response: Response) -> AdminSessionCreated:
        try:
            created = control.create_admin_session(body.access_token)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
            ) from exc
        _set_session_cookie(response, created.session_token)
        return _created_session_document(created)

    @app.get("/api/v1/admin/session", response_model=AdminSessionView)
    async def current_admin_session(request: Request) -> AdminSessionView:
        session = _authenticate_request(control, request, require_csrf=False)
        return _session_view(session)

    @app.get("/api/v1/admin/tenants", response_model=TenantListView)
    async def list_admin_tenants(request: Request) -> TenantListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, TENANT_VISIBLE_ROLES)
        return TenantListView(
            tenants=tuple(
                TenantView(
                    tenant_id=tenant.tenant_id,
                    name=tenant.name,
                    active=tenant.active,
                )
                for tenant in control.list_tenants()
            )
        )

    @app.get("/api/v1/admin/operators", response_model=OperatorListView)
    async def list_admin_operators(request: Request) -> OperatorListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        return OperatorListView(
            operators=tuple(_operator_view(operator) for operator in control.list_operators())
        )

    @app.post(
        "/api/v1/admin/operators",
        response_model=OperatorView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_admin_operator(
        body: CreateOperatorBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> OperatorView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        try:
            operator = control.create_operator(
                body.operator_id,
                body.name,
                frozenset(body.roles),
            )
        except ControlConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="operator.create",
            target_type="operator",
            target_ids={"operator_id": operator.operator_id},
            after_metadata={"roles": ",".join(sorted(operator.roles))},
        )
        return _operator_view(operator)

    @app.patch("/api/v1/admin/operators/{operator_id}", response_model=OperatorView)
    async def update_admin_operator(
        operator_id: str,
        body: UpdateOperatorBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> OperatorView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        try:
            operator = control.update_operator(
                operator_id,
                name=body.name,
                roles=None if body.roles is None else frozenset(body.roles),
                active=body.active,
            )
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="operator.update",
            target_type="operator",
            target_ids={"operator_id": operator.operator_id},
            after_metadata={
                "active": str(operator.active).lower(),
                "roles": ",".join(sorted(operator.roles)),
            },
        )
        return _operator_view(operator)

    @app.get(
        "/api/v1/admin/operators/{operator_id}/tokens",
        response_model=OperatorTokenListView,
    )
    async def list_admin_operator_tokens(
        operator_id: str,
        request: Request,
    ) -> OperatorTokenListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        try:
            tokens = control.list_operator_tokens(operator_id)
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return OperatorTokenListView(
            tokens=tuple(_operator_token_record_view(token) for token in tokens)
        )

    @app.post(
        "/api/v1/admin/operators/{operator_id}/tokens",
        response_model=CredentialView,
        status_code=status.HTTP_201_CREATED,
    )
    async def issue_admin_operator_token(
        operator_id: str,
        body: IssueOperatorTokenBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> CredentialView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        try:
            credential = control.issue_operator_access_token(
                operator_id,
                lifetime=None if body.lifetime_days is None else timedelta(days=body.lifetime_days),
            )
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="operator_token.issue",
            target_type="operator_access_token",
            target_ids={"operator_id": operator_id, "token_id": credential.token_id},
        )
        return _credential_view(credential)

    @app.post(
        "/api/v1/admin/operator-tokens/{token_id}/rotate",
        response_model=RotatedCredentialView,
    )
    async def rotate_admin_operator_token(
        token_id: str,
        body: RotateOperatorTokenBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> RotatedCredentialView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        try:
            rotated = control.rotate_operator_access_token(
                token_id,
                overlap=timedelta(minutes=body.overlap_minutes),
                lifetime=None if body.lifetime_days is None else timedelta(days=body.lifetime_days),
            )
        except (AuthenticationError, ControlNotFound) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="operator_token.rotate",
            target_type="operator_access_token",
            target_ids={"token_id": token_id, "replacement_token_id": rotated.credential.token_id},
            after_metadata={"overlap_minutes": str(body.overlap_minutes)},
        )
        return _rotated_credential_view(rotated)

    @app.delete(
        "/api/v1/admin/operator-tokens/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke_admin_operator_token(
        token_id: str,
        request: Request,
        response: Response,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> Response:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, OPERATOR_ADMIN_ROLES)
        control.revoke_operator_access_token(token_id)
        control.record_operator_audit_event(
            session,
            action="operator_token.revoke",
            target_type="operator_access_token",
            target_ids={"token_id": token_id},
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.get("/api/v1/admin/principals", response_model=PrincipalListView)
    async def list_admin_principals(request: Request) -> PrincipalListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, IDENTITY_VISIBLE_ROLES)
        tenants = control.list_tenants()
        return PrincipalListView(
            principals=tuple(_principal_view(principal) for principal in control.list_principals()),
            memberships=tuple(
                _membership_view(membership)
                for tenant in tenants
                for membership in control.list_memberships(tenant.tenant_id)
            ),
        )

    @app.post(
        "/api/v1/admin/users",
        response_model=CreatedUserView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_admin_user(
        body: CreateUserBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> CreatedUserView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, IDENTITY_MUTATION_ROLES)
        try:
            principal = control.create_principal(body.principal_id, body.name, "user")
            membership = None
            for role in body.roles:
                membership = control.grant_membership(body.tenant_id, body.principal_id, role)
        except ControlConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if membership is None:  # pragma: no cover - pydantic enforces at least one role
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing roles")
        lifetime = (
            None if body.token_lifetime_days is None else timedelta(days=body.token_lifetime_days)
        )
        try:
            credential = (
                control.issue_access_token(body.tenant_id, body.principal_id, lifetime=lifetime)
                if body.issue_token
                else None
            )
        except (ControlNotFound, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="principal.create_user",
            target_type="principal",
            target_ids={
                "tenant_id": body.tenant_id,
                "principal_id": body.principal_id,
            },
            after_metadata={
                "roles": ",".join(sorted(membership.roles)),
                "issued_token": str(credential is not None).lower(),
            },
        )
        return CreatedUserView(
            principal=_principal_view(principal),
            membership=_membership_view(membership),
            credential=None if credential is None else _credential_view(credential),
        )

    @app.get("/api/v1/admin/tokens", response_model=TokenListView)
    async def list_admin_tokens(
        tenant_id: str,
        principal_id: str,
        request: Request,
    ) -> TokenListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, TOKEN_VISIBLE_ROLES)
        return TokenListView(
            tokens=tuple(
                _token_record_view(token) for token in control.list_tokens(tenant_id, principal_id)
            )
        )

    @app.post(
        "/api/v1/admin/tokens",
        response_model=CredentialView,
        status_code=status.HTTP_201_CREATED,
    )
    async def issue_admin_token(
        body: IssueTokenBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> CredentialView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, TOKEN_MUTATION_ROLES)
        try:
            credential = control.issue_access_token(
                body.tenant_id,
                body.principal_id,
                lifetime=None if body.lifetime_days is None else timedelta(days=body.lifetime_days),
            )
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="access_token.issue",
            target_type="access_token",
            target_ids={
                "tenant_id": body.tenant_id,
                "principal_id": body.principal_id,
                "token_id": credential.token_id,
            },
        )
        return _credential_view(credential)

    @app.post("/api/v1/admin/tokens/{token_id}/rotate", response_model=RotatedCredentialView)
    async def rotate_admin_token(
        token_id: str,
        body: RotateTokenBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> RotatedCredentialView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, TOKEN_MUTATION_ROLES)
        try:
            rotated = control.rotate_access_token(
                token_id,
                overlap=timedelta(minutes=body.overlap_minutes),
                lifetime=None if body.lifetime_days is None else timedelta(days=body.lifetime_days),
            )
        except (AuthenticationError, ControlNotFound) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        control.record_operator_audit_event(
            session,
            action="access_token.rotate",
            target_type="access_token",
            target_ids={"token_id": token_id, "replacement_token_id": rotated.credential.token_id},
            after_metadata={"overlap_minutes": str(body.overlap_minutes)},
        )
        return _rotated_credential_view(rotated)

    @app.delete("/api/v1/admin/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_admin_token(
        token_id: str,
        request: Request,
        response: Response,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> Response:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, TOKEN_MUTATION_ROLES)
        control.revoke_access_token(token_id)
        control.record_operator_audit_event(
            session,
            action="access_token.revoke",
            target_type="access_token",
            target_ids={"token_id": token_id},
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.post(
        "/api/v1/admin/provisioning-jobs",
        response_model=ProvisioningJobView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_admin_provisioning_job(
        body: CreateProvisioningJobBody,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> ProvisioningJobView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, PROVISIONING_MUTATION_ROLES)
        try:
            job = control.create_provisioning_job(
                session,
                body.manifest,
                idempotency_key=body.idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return provisioning_job_view(job)

    @app.get("/api/v1/admin/provisioning-jobs", response_model=ProvisioningJobListView)
    async def list_admin_provisioning_jobs(request: Request) -> ProvisioningJobListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, PROVISIONING_VISIBLE_ROLES)
        return ProvisioningJobListView(
            jobs=tuple(provisioning_job_view(job) for job in control.list_provisioning_jobs())
        )

    @app.post(
        "/api/v1/admin/provisioning-jobs/{job_id}/cancel",
        response_model=ProvisioningJobView,
    )
    async def cancel_admin_provisioning_job(
        job_id: str,
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> ProvisioningJobView:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        _require_roles(session, PROVISIONING_MUTATION_ROLES)
        try:
            job = control.cancel_provisioning_job(session, job_id)
        except ControlNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return provisioning_job_view(job)

    @app.get("/api/v1/admin/audit-events", response_model=OperatorAuditEventListView)
    async def list_operator_audit_events(request: Request) -> OperatorAuditEventListView:
        session = _authenticate_request(control, request, require_csrf=False)
        _require_roles(session, AUDIT_VISIBLE_ROLES)
        return OperatorAuditEventListView(
            events=tuple(
                operator_audit_event_view(event)
                for event in control.list_operator_audit_events(limit=100)
            )
        )

    @app.delete("/api/v1/admin/session", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_admin_session(
        request: Request,
        response: Response,
        csrf_token: Annotated[str | None, Header(alias=ADMIN_CSRF_HEADER)] = None,
    ) -> Response:
        session = _authenticate_request(
            control,
            request,
            csrf_token=csrf_token,
            require_csrf=True,
        )
        if session.session_id is not None:
            control.revoke_admin_session(session.session_id)
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/api/v1/admin")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response


def _authenticate_request(
    control: ControlModule,
    request: Request,
    *,
    csrf_token: str | None = None,
    require_csrf: bool,
) -> OperatorSession:
    session_token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    try:
        return control.authenticate_admin_session(
            session_token,
            csrf_token=csrf_token,
            require_csrf=require_csrf,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
        ) from exc


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_token,
        max_age=ADMIN_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/api/v1/admin",
    )


def _created_session_document(created: IssuedAdminSession) -> AdminSessionCreated:
    return AdminSessionCreated(
        session_id=created.session_id,
        operator_id=created.session.operator_id,
        roles=tuple(sorted(created.session.roles)),
        csrf_token=created.csrf_token,
        absolute_expires_at=created.absolute_expires_at.isoformat(),
        idle_expires_at=created.idle_expires_at.isoformat(),
    )


def _session_view(session: OperatorSession) -> AdminSessionView:
    if session.session_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return AdminSessionView(
        session_id=session.session_id,
        operator_id=session.operator_id,
        roles=tuple(sorted(session.roles)),
    )


def _operator_view(operator: OperatorRecord) -> OperatorView:
    return OperatorView(
        operator_id=operator.operator_id,
        name=operator.name,
        roles=tuple(sorted(operator.roles)),
        active=operator.active,
    )


def _principal_view(principal: PrincipalRecord) -> PrincipalView:
    return PrincipalView(
        principal_id=principal.principal_id,
        name=principal.name,
        kind=principal.kind.value,
        active=principal.active,
    )


def _membership_view(membership: MembershipRecord) -> MembershipView:
    return MembershipView(
        tenant_id=membership.tenant_id,
        principal_id=membership.principal_id,
        roles=tuple(sorted(membership.roles)),
        active=membership.active,
    )


def _token_record_view(record: TokenRecord) -> TokenRecordView:
    checked_at = datetime.now(UTC)
    return TokenRecordView(
        token_id=record.token_id,
        tenant_id=record.session.tenant_id,
        principal_id=record.session.actor_id,
        actor_kind=record.session.actor_kind.value,
        roles=tuple(sorted(record.session.roles)),
        subject_user_id=record.session.subject_user_id,
        delegation_id=record.session.delegation_id,
        issued_at=record.issued_at.isoformat(),
        expires_at=record.expires_at.isoformat(),
        revoked_at=None if record.revoked_at is None else record.revoked_at.isoformat(),
        last_used_at=None if record.last_used_at is None else record.last_used_at.isoformat(),
        active=record.revoked_at is None and record.expires_at > checked_at,
    )


def _operator_token_record_view(record: OperatorTokenRecord) -> OperatorTokenRecordView:
    checked_at = datetime.now(UTC)
    return OperatorTokenRecordView(
        token_id=record.token_id,
        operator_id=record.session.operator_id,
        roles=tuple(sorted(record.session.roles)),
        issued_at=record.issued_at.isoformat(),
        expires_at=record.expires_at.isoformat(),
        revoked_at=None if record.revoked_at is None else record.revoked_at.isoformat(),
        last_used_at=None if record.last_used_at is None else record.last_used_at.isoformat(),
        active=record.revoked_at is None and record.expires_at > checked_at,
    )


def _require_roles(session: OperatorSession, allowed: frozenset[str]) -> None:
    if not session.roles.intersection(allowed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _credential_view(credential: object) -> CredentialView:
    from agent_memory_service.auth import IssuedCredential

    if not isinstance(credential, IssuedCredential):
        raise TypeError("Expected an issued credential")
    return CredentialView(
        token_id=credential.token_id,
        access_token=credential.access_token,
        expires_at=credential.expires_at.isoformat(),
        warning="This Access Token is shown once; store it securely.",
    )


def _rotated_credential_view(rotated: RotatedCredential) -> RotatedCredentialView:
    return RotatedCredentialView(
        previous_token_id=rotated.previous_token_id,
        previous_valid_until=rotated.previous_valid_until.isoformat(),
        credential=_credential_view(rotated.credential),
    )
