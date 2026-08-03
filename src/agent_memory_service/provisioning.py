"""Resumable host-side Tenant provisioning Interface and Adapters."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from agent_memory_service.manifest import TenantManifest


class ProvisioningConflict(RuntimeError):
    """The requested apply cannot safely continue from recorded state."""


class ProvisioningFailed(RuntimeError):
    """One resumable provisioning step failed."""

    def __init__(self, step: ProvisioningStep) -> None:
        self.step = step
        super().__init__(f"Tenant provisioning failed at step: {step.value}")


class ProvisioningStatus(StrEnum):
    NOT_STARTED = "not_started"
    PROVISIONING = "provisioning"
    FAILED = "failed"
    ACTIVE = "active"


class ProvisioningStep(StrEnum):
    RECORD_CONTROL_STATE = "record_control_state"
    WRITE_SECRETS = "write_secrets"
    START_NEO4J = "start_neo4j"
    CREATE_TENANT_DATABASE = "create_tenant_database"
    MIGRATE_POSTGRES = "migrate_postgres"
    MIGRATE_NEO4J = "migrate_neo4j"
    VERIFY_NEO4J_HEALTH = "verify_neo4j_health"
    VERIFY_ROUTING = "verify_routing"
    VERIFY_ISOLATION = "verify_isolation"
    ACTIVATE = "activate"


PROVISIONING_STEPS = tuple(ProvisioningStep)
_SECRET_NAMES = ("neo4j_password", "postgres_password")
TENANT_SECRET_FILE_MODE = 0o640
TENANT_SECRET_DIRECTORY_MODE = 0o750
_STEP_ACTIONS = {
    ProvisioningStep.RECORD_CONTROL_STATE: (
        "record the inactive Tenant, declared Principals, Memberships, policies, and route"
    ),
    ProvisioningStep.WRITE_SECRETS: "create missing protected Tenant secret files",
    ProvisioningStep.START_NEO4J: "start the isolated Neo4j Community Compose project",
    ProvisioningStep.CREATE_TENANT_DATABASE: "create the Tenant database and distinct role",
    ProvisioningStep.MIGRATE_POSTGRES: "apply Tenant PostgreSQL schema migrations",
    ProvisioningStep.MIGRATE_NEO4J: "apply idempotent Neo4j schema migrations",
    ProvisioningStep.VERIFY_NEO4J_HEALTH: "verify the Tenant Memory Store is healthy",
    ProvisioningStep.VERIFY_ROUTING: "verify server-derived routing reaches this Tenant",
    ProvisioningStep.VERIFY_ISOLATION: "verify this Tenant cannot observe another Tenant",
    ProvisioningStep.ACTIVATE: "mark the Tenant active in the Control Store",
}


@dataclass(frozen=True, slots=True)
class ProvisioningState:
    tenant_id: str
    manifest_fingerprint: str
    database_name: str
    database_role: str
    neo4j_service_name: str
    status: ProvisioningStatus
    completed_steps: tuple[ProvisioningStep, ...] = ()
    failed_step: ProvisioningStep | None = None
    failure_code: str | None = None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class PlannedStep:
    step: ProvisioningStep
    action: str
    completed: bool


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    tenant_id: str
    manifest_fingerprint: str
    current_status: ProvisioningStatus
    steps: tuple[PlannedStep, ...]

    @property
    def pending_steps(self) -> tuple[ProvisioningStep, ...]:
        return tuple(item.step for item in self.steps if not item.completed)

    @property
    def is_noop(self) -> bool:
        return not self.pending_steps


class ProvisioningStateStore(Protocol):
    """Durable, content-free progress store; implementations enforce transitions.

    ``start`` atomically records the validated manifest as inactive and returns state
    with ``RECORD_CONTROL_STATE`` completed. No domain memory or credential is stored.
    """

    def get(self, tenant_id: str) -> ProvisioningState | None: ...

    def start(self, manifest: TenantManifest) -> ProvisioningState: ...

    def mark_completed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
    ) -> ProvisioningState: ...

    def mark_failed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
        failure_code: str,
    ) -> ProvisioningState: ...

    def activate(self, tenant_id: str) -> ProvisioningState: ...


class SecretWriter(Protocol):
    """Protected secret-file Adapter. Existing files must never be overwritten."""

    def ensure_secret(self, tenant_id: str, name: str, value: str, *, mode: int) -> None: ...


class ComposeRunner(Protocol):
    """Host-side Compose Adapter; application containers never receive Docker access."""

    def ensure_tenant_running(self, manifest: TenantManifest) -> None:
        """Idempotently start the Tenant's isolated Neo4j Community project."""


class TenantDatabaseProvisioner(Protocol):
    def ensure_database_and_role(
        self,
        manifest: TenantManifest,
        *,
        password_secret_name: str,
    ) -> None:
        """Idempotently create the Tenant database and its distinct role."""


class TenantMigrationRunner(Protocol):
    def migrate_postgres(self, manifest: TenantManifest) -> None: ...

    def migrate_neo4j(self, manifest: TenantManifest) -> None: ...


class TenantProvisioningVerifier(Protocol):
    def verify_neo4j_health(self, manifest: TenantManifest) -> None: ...

    def verify_routing(self, manifest: TenantManifest) -> None: ...

    def verify_isolation(self, manifest: TenantManifest) -> None: ...


class InMemoryProvisioningStateStore:
    """Strict deterministic Adapter for tests and local operator workflows."""

    def __init__(self) -> None:
        self._states: dict[str, ProvisioningState] = {}

    def get(self, tenant_id: str) -> ProvisioningState | None:
        return self._states.get(tenant_id)

    def start(self, manifest: TenantManifest) -> ProvisioningState:
        current = self._states.get(manifest.tenant_id)
        if current is None:
            state = ProvisioningState(
                tenant_id=manifest.tenant_id,
                manifest_fingerprint=manifest.fingerprint,
                database_name=manifest.database_name,
                database_role=manifest.database_role,
                neo4j_service_name=manifest.neo4j_service_name,
                status=ProvisioningStatus.PROVISIONING,
                completed_steps=(ProvisioningStep.RECORD_CONTROL_STATE,),
                attempt=1,
            )
        else:
            _require_matching_manifest(current, manifest.fingerprint)
            if current.status is ProvisioningStatus.ACTIVE:
                return current
            _validate_completed_prefix(current)
            state = replace(
                current,
                status=ProvisioningStatus.PROVISIONING,
                failed_step=None,
                failure_code=None,
                attempt=current.attempt + 1,
            )
        self._states[manifest.tenant_id] = state
        return state

    def mark_completed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
    ) -> ProvisioningState:
        current = self._require_provisioning(tenant_id)
        expected = _next_step(current.completed_steps)
        if step is ProvisioningStep.ACTIVATE:
            raise ProvisioningConflict("Activation requires the final activation transition")
        if step is not expected:
            raise ProvisioningConflict("Provisioning steps must complete in order")
        state = replace(current, completed_steps=(*current.completed_steps, step))
        self._states[tenant_id] = state
        return state

    def mark_failed(
        self,
        tenant_id: str,
        step: ProvisioningStep,
        failure_code: str,
    ) -> ProvisioningState:
        current = self._require_provisioning(tenant_id)
        if step is not _next_step(current.completed_steps):
            raise ProvisioningConflict("Only the current provisioning step can fail")
        state = replace(
            current,
            status=ProvisioningStatus.FAILED,
            failed_step=step,
            failure_code=failure_code,
        )
        self._states[tenant_id] = state
        return state

    def activate(self, tenant_id: str) -> ProvisioningState:
        current = self._require_provisioning(tenant_id)
        if _next_step(current.completed_steps) is not ProvisioningStep.ACTIVATE:
            raise ProvisioningConflict("Tenant cannot activate before every verification passes")
        state = replace(
            current,
            status=ProvisioningStatus.ACTIVE,
            completed_steps=(*current.completed_steps, ProvisioningStep.ACTIVATE),
        )
        self._states[tenant_id] = state
        return state

    def _require_provisioning(self, tenant_id: str) -> ProvisioningState:
        state = self._states.get(tenant_id)
        if state is None:
            raise ProvisioningConflict("Tenant provisioning has not started")
        if state.status is not ProvisioningStatus.PROVISIONING:
            raise ProvisioningConflict("Tenant is not in the provisioning state")
        _validate_completed_prefix(state)
        return state


class FilesystemSecretWriter:
    """Create group-readable secrets for the non-root application containers."""

    def __init__(self, root: Path, *, group_id: int | None = None) -> None:
        if group_id is not None and group_id < 1:
            raise ValueError("Tenant secret group ID must be positive")
        self._root = root
        self._group_id = os.getegid() if group_id is None else group_id

    def ensure_secret(self, tenant_id: str, name: str, value: str, *, mode: int) -> None:
        if mode != TENANT_SECRET_FILE_MODE:
            raise ValueError("Tenant secret files must use mode 0640")
        safe_tenant_characters = "abcdefghijklmnopqrstuvwxyz0123456789-"
        if not tenant_id or any(character not in safe_tenant_characters for character in tenant_id):
            raise ValueError("Unsafe Tenant secret directory name")
        safe_name_characters = "abcdefghijklmnopqrstuvwxyz0123456789_"
        if not name or any(character not in safe_name_characters for character in name):
            raise ValueError("Unsafe Tenant secret file name")
        self._root.mkdir(mode=TENANT_SECRET_DIRECTORY_MODE, parents=True, exist_ok=True)
        _secure_secret_directory(self._root, self._group_id)
        tenant_directory = self._root / tenant_id
        tenant_directory.mkdir(
            mode=TENANT_SECRET_DIRECTORY_MODE,
            parents=False,
            exist_ok=True,
        )
        if tenant_directory.is_symlink() or not tenant_directory.is_dir():
            raise ValueError("Tenant secret directory must be a real directory")
        _secure_secret_directory(tenant_directory, self._group_id)
        destination = tenant_directory / name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(destination, flags, mode)
        except FileExistsError:
            _validate_existing_secret(destination, mode, self._group_id)
            return
        try:
            os.fchown(descriptor, -1, self._group_id)
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
                secret_file.write(value)
                secret_file.write("\n")
                secret_file.flush()
                os.fsync(secret_file.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


class TenantProvisioner:
    """Deep orchestration Module for declarative, resumable Tenant provisioning."""

    def __init__(
        self,
        states: ProvisioningStateStore,
        secrets_writer: SecretWriter,
        compose: ComposeRunner,
        database: TenantDatabaseProvisioner,
        migrations: TenantMigrationRunner,
        verifier: TenantProvisioningVerifier,
    ) -> None:
        self._states = states
        self._secrets = secrets_writer
        self._compose = compose
        self._database = database
        self._migrations = migrations
        self._verifier = verifier

    def plan(self, manifest: TenantManifest) -> ProvisioningPlan:
        """Return the complete plan without mutating infrastructure or progress."""
        state = self._states.get(manifest.tenant_id)
        if state is None:
            status = ProvisioningStatus.NOT_STARTED
            completed: frozenset[ProvisioningStep] = frozenset()
        else:
            _require_matching_manifest(state, manifest.fingerprint)
            _validate_completed_prefix(state)
            status = state.status
            completed = frozenset(state.completed_steps)
        return ProvisioningPlan(
            tenant_id=manifest.tenant_id,
            manifest_fingerprint=manifest.fingerprint,
            current_status=status,
            steps=tuple(
                PlannedStep(
                    step=step,
                    action=_STEP_ACTIONS[step],
                    completed=step in completed,
                )
                for step in PROVISIONING_STEPS
            ),
        )

    def apply(self, manifest: TenantManifest) -> ProvisioningState:
        """Apply pending steps, persisting each boundary so a later call can resume."""
        plan = self.plan(manifest)
        if plan.is_noop:
            active = self._states.get(manifest.tenant_id)
            if active is None:  # pragma: no cover - guarded by a complete plan
                raise ProvisioningConflict("Completed plan has no provisioning state")
            return active

        state = self._states.start(manifest)
        if state.completed_steps[:1] != (ProvisioningStep.RECORD_CONTROL_STATE,):
            raise ProvisioningConflict("Provisioning state did not record the Tenant manifest")
        completed = frozenset(state.completed_steps)
        for step in PROVISIONING_STEPS:
            if step in completed:
                continue
            try:
                if step is ProvisioningStep.ACTIVATE:
                    return self._states.activate(manifest.tenant_id)
                self._execute(step, manifest)
                state = self._states.mark_completed(manifest.tenant_id, step)
                completed = frozenset(state.completed_steps)
            except Exception as exc:
                failure_code = type(exc).__name__
                self._states.mark_failed(manifest.tenant_id, step, failure_code)
                raise ProvisioningFailed(step) from exc
        raise ProvisioningConflict("Provisioning ended without activation")

    def _execute(self, step: ProvisioningStep, manifest: TenantManifest) -> None:
        if step is ProvisioningStep.WRITE_SECRETS:
            for name in _SECRET_NAMES:
                self._secrets.ensure_secret(
                    manifest.tenant_id,
                    name,
                    f"c{secrets.token_urlsafe(48)}",
                    mode=TENANT_SECRET_FILE_MODE,
                )
        elif step is ProvisioningStep.START_NEO4J:
            self._compose.ensure_tenant_running(manifest)
        elif step is ProvisioningStep.CREATE_TENANT_DATABASE:
            self._database.ensure_database_and_role(
                manifest,
                password_secret_name="postgres_password",
            )
        elif step is ProvisioningStep.MIGRATE_POSTGRES:
            self._migrations.migrate_postgres(manifest)
        elif step is ProvisioningStep.MIGRATE_NEO4J:
            self._migrations.migrate_neo4j(manifest)
        elif step is ProvisioningStep.VERIFY_NEO4J_HEALTH:
            self._verifier.verify_neo4j_health(manifest)
        elif step is ProvisioningStep.VERIFY_ROUTING:
            self._verifier.verify_routing(manifest)
        elif step is ProvisioningStep.VERIFY_ISOLATION:
            self._verifier.verify_isolation(manifest)
        else:  # pragma: no cover - activation is handled transactionally by the store
            raise ProvisioningConflict("Unknown provisioning step")


def _next_step(completed_steps: tuple[ProvisioningStep, ...]) -> ProvisioningStep | None:
    if len(completed_steps) >= len(PROVISIONING_STEPS):
        return None
    return PROVISIONING_STEPS[len(completed_steps)]


def _validate_completed_prefix(state: ProvisioningState) -> None:
    expected = PROVISIONING_STEPS[: len(state.completed_steps)]
    if state.completed_steps != expected:
        raise ProvisioningConflict("Provisioning progress is not a valid completed prefix")
    activated = ProvisioningStep.ACTIVATE in state.completed_steps
    if activated != (state.status is ProvisioningStatus.ACTIVE):
        raise ProvisioningConflict("Provisioning activation state is inconsistent")


def _require_matching_manifest(state: ProvisioningState, fingerprint: str) -> None:
    if state.manifest_fingerprint != fingerprint:
        raise ProvisioningConflict("Cannot resume Tenant provisioning with a changed manifest")


def _secure_secret_directory(directory: Path, group_id: int) -> None:
    details = directory.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("Tenant secret directory must be a real directory")
    os.chown(directory, -1, group_id, follow_symlinks=False)
    os.chmod(directory, TENANT_SECRET_DIRECTORY_MODE, follow_symlinks=False)


def _validate_existing_secret(destination: Path, mode: int, group_id: int) -> None:
    details = destination.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("Existing Tenant secret must be a regular file")
    if stat.S_IMODE(details.st_mode) != mode:
        raise PermissionError("Existing Tenant secret must use mode 0640")
    if details.st_gid != group_id:
        raise PermissionError("Existing Tenant secret must use the configured group")
    if details.st_size == 0:
        raise ValueError("Existing Tenant secret cannot be empty")
