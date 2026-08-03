from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from activegraph.core.event import Event  # type: ignore[import-untyped]

from agent_memory_service.agents import (
    AgentBudget,
    AgentInvocation,
    AgentModelSettings,
    AgentRunEvent,
    AgentRunSnapshot,
    AgentRunState,
)
from agent_memory_service.archive import ArchiveScope, encode_archive
from agent_memory_service.auth import TokenService
from agent_memory_service.backup import (
    BackupArtifactManifest,
    BackupManifest,
    BackupStore,
    BackupValidationError,
    EncryptionCommandFailed,
    EncryptionMetadata,
    PrivateMemoryRestoreProof,
    RestoreExpectations,
    RestoreProofTarget,
    RestoreSafetyError,
    activegraph_event_sequence_digest,
    content_safe_digest,
    private_memory_correction_digest,
    private_memory_erasure_digest,
    private_memory_item_digest,
    sha256_file,
    tenant_knowledge_digest,
)
from agent_memory_service.control import ControlModule, InMemoryControlStore, TenantRouteRecord
from agent_memory_service.host_restore import HostRestoreConfig, HostRestoreDrillExecutor
from agent_memory_service.lifecycle import ErasureTombstone
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import (
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemoryState,
    MutationState,
    PrincipalKind,
    PrivateMemoryInspection,
    Provenance,
    RecallResult,
    TenantSession,
)
from agent_memory_service.restore_verify import (
    CanonicalRestoreProbe,
    DockerCanonicalRestoreVerifier,
    RestoreVerificationContext,
    RestoreVerificationReport,
    run_restore_checks,
)
from agent_memory_service.schema import CONTROL_SCHEMA_REVISION, TENANT_SCHEMA_REVISION
from agent_memory_service.stores.memory import (
    InMemoryTenantMemoryRouter,
    TenantMemoryUnavailable,
)

STARTED = datetime(2026, 8, 2, 10, tzinfo=UTC)
COMPLETED = datetime(2026, 8, 2, 10, 42, tzinfo=UTC)
KNOWLEDGE_CLAIM = "restore availability verification marker"


def _expectations() -> RestoreExpectations:
    empty_private_target = RestoreProofTarget(
        count=0,
        digest=content_safe_digest(()),
    )
    return RestoreExpectations(
        tenant_knowledge_digests=(
            tenant_knowledge_digest(
                content=KNOWLEDGE_CLAIM,
                confidence=0.9,
                actor_id="agent-a",
                source="candidate:candidate-a",
            ),
        ),
        governance_candidate_count=0,
        governance_digest=content_safe_digest([]),
        private_memory=PrivateMemoryRestoreProof(
            active=empty_private_target,
            correction_chain=empty_private_target,
            completed_erasure=empty_private_target,
        ),
        agent_run_count=0,
    )


class RecordingRunner:
    def __init__(self, *, verification_report: RestoreVerificationReport | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str] | None] = []
        self.verification_report = verification_report

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        command = tuple(arguments)
        self.commands.append(command)
        self.environments.append(environment)
        if command[0] == "age":
            source = Path(command[-1])
            destination = Path(command[command.index("--output") + 1])
            destination.write_bytes(b"plain:" + source.read_bytes())
        if (
            self.verification_report is not None
            and command[:2] == ("docker", "run")
            and "agent_memory_service.restore_verify" in command
        ):
            workspace_mount = next(
                value
                for index, value in enumerate(command)
                if command[index - 1] == "--volume" and value.endswith(":/run/restore:rw")
            )
            workspace = Path(workspace_mount.removesuffix(":/run/restore:rw"))
            (workspace / "verification.json").write_text(
                self.verification_report.model_dump_json(),
                encoding="utf-8",
            )

    def remove_docker_resource(
        self,
        kind: str,
        name: str,
    ) -> None:
        prefixes = {
            "container": ("docker", "rm", "--force"),
            "volume": ("docker", "volume", "rm"),
            "network": ("docker", "network", "rm"),
        }
        self.commands.append((*prefixes[kind], name))
        self.environments.append(None)


class FixedVerifier:
    def __init__(self, report: RestoreVerificationReport) -> None:
        self.report = report
        self.contexts: list[RestoreVerificationContext] = []

    def verify(self, context: RestoreVerificationContext) -> RestoreVerificationReport:
        self.contexts.append(context)
        return self.report


def _protected_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _manifest(tmp_path: Path) -> tuple[BackupManifest, dict[str, Path]]:
    artifacts: list[BackupArtifactManifest] = []
    paths: dict[str, Path] = {}
    versions = {
        BackupStore.CONTROL_POSTGRES: ("17.6-alpine", CONTROL_SCHEMA_REVISION),
        BackupStore.TENANT_POSTGRES: (
            "17.6-alpine",
            TENANT_SCHEMA_REVISION,
        ),
        BackupStore.TENANT_NEO4J: ("5.26.28-community", "1"),
    }
    for store in BackupStore:
        path = tmp_path / f"{store.value}.age"
        path.write_bytes(f"encrypted:{store.value}".encode())
        artifact_id = f"backup-001:{store.value}"
        store_version, schema_version = versions[store]
        artifacts.append(
            BackupArtifactManifest(
                artifact_id=artifact_id,
                object_key=f"backup-001/{store.value}.age",
                store=store,
                tenant_id=None if store is BackupStore.CONTROL_POSTGRES else "tenant-a",
                store_version=store_version,
                schema_version=schema_version,
                created_at=STARTED,
                ciphertext_sha256=sha256_file(path),
                ciphertext_bytes=path.stat().st_size,
                encryption=EncryptionMetadata(key_id="restore-key-1"),
                complete=True,
            )
        )
        paths[artifact_id] = path
    return (
        BackupManifest(
            backup_id="backup-001",
            tenant_id="tenant-a",
            created_at=STARTED,
            database_name="tenant_tenant_a",
            database_role="tenant_tenant_a_rw",
            neo4j_service_name="neo4j-tenant-a",
            expectations=_expectations(),
            artifacts=tuple(artifacts),
            complete=True,
        ),
        paths,
    )


def _executor(
    tmp_path: Path,
    runner: RecordingRunner,
    verifier: FixedVerifier,
) -> HostRestoreDrillExecutor:
    workspace_root = tmp_path / "restore-workspaces"
    workspace_root.mkdir()
    times = iter((STARTED, COMPLETED))
    secrets = iter(("postgres-super-secret", "tenant-postgres-secret", "neo4j-secret-value"))
    return HostRestoreDrillExecutor(
        runner=runner,
        verifier=verifier,
        config=HostRestoreConfig(workspace_root=workspace_root),
        clock=lambda: next(times),
        sleeper=lambda _seconds: None,
        secret_factory=lambda: next(secrets),
    )


def test_successful_restore_is_internal_fixed_argv_and_cleans_exact_resources(
    tmp_path: Path,
) -> None:
    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    report = RestoreVerificationReport(
        public_recall=True,
        private_memory=True,
        governance=True,
        agent_runs=True,
        tenant_isolation=True,
    )
    runner = RecordingRunner()
    verifier = FixedVerifier(report)
    executor = _executor(tmp_path, runner, verifier)

    record = executor.execute(
        manifest=manifest,
        ciphertext_paths=ciphertext_paths,
        target_id="restore-drill-tenant-a-001",
        operator_id="operator-alice",
        age_identity_file=identity,
    )

    assert record.outcome == "passed"
    assert record.duration_seconds == 42 * 60
    assert record.within_rto is True
    assert record.passed_checks == (
        "public-recall",
        "private-memory",
        "governance",
        "agent-runs",
        "tenant-isolation",
    )
    assert runner.commands[3] == (
        "docker",
        "network",
        "create",
        "--internal",
        "--label",
        "memory.restore.target=restore-drill-tenant-a-001",
        "memory-restore-drill-tenant-a-001",
    )
    command_text = "\n".join(" ".join(command) for command in runner.commands)
    assert "--publish" not in command_text
    assert " -p " not in f" {command_text} "
    assert "/bin/sh" not in command_text
    assert "postgres-super-secret" not in command_text
    assert "tenant-postgres-secret" not in command_text
    assert "neo4j-secret-value" not in command_text
    assert "--network-alias neo4j-tenant-a" in command_text
    loader = next(
        command for command in runner.commands if "database" in command and "load" in command
    )
    assert loader[loader.index("--cap-drop") + 1] == "ALL"
    assert loader[loader.index("--user") + 1] == f"7474:{os.getgid()}"
    assert f"/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777,uid=7474,gid={os.getgid()}" in loader
    assert f"/logs:rw,nosuid,nodev,noexec,size=64m,mode=0750,uid=7474,gid={os.getgid()}" in loader
    assert loader[loader.index("--entrypoint") + 1] == ("/var/lib/neo4j/bin/neo4j-admin")
    assert "neo4j-admin" not in loader[loader.index("--entrypoint") + 2 :]
    assert any(
        command[:4] == ("docker", "rm", "--force", "memory-restore-drill-tenant-a-001-neo4j")
        for command in runner.commands
    )
    assert any(
        command
        == (
            "docker",
            "rm",
            "--force",
            "memory-restore-drill-tenant-a-001-verifier",
        )
        for command in runner.commands
    )
    assert any(
        command == ("docker", "network", "rm", "memory-restore-drill-tenant-a-001")
        for command in runner.commands
    )
    assert not (tmp_path / "restore-workspaces" / "restore-drill-tenant-a-001").exists()
    assert verifier.contexts[0].network_name == "memory-restore-drill-tenant-a-001"


def test_failed_verification_preserves_exact_resources_and_protected_secrets(
    tmp_path: Path,
) -> None:
    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    runner = RecordingRunner()
    verifier = FixedVerifier(
        RestoreVerificationReport(
            public_recall=True,
            private_memory=True,
            governance=False,
            agent_runs=True,
            tenant_isolation=True,
        )
    )
    executor = _executor(tmp_path, runner, verifier)

    record = executor.execute(
        manifest=manifest,
        ciphertext_paths=ciphertext_paths,
        target_id="restore-drill-tenant-a-failed",
        operator_id="operator-alice",
        age_identity_file=identity,
    )

    assert record.outcome == "failed"
    assert record.within_rto is False
    assert "governance" not in record.passed_checks
    assert not any(command[:2] == ("docker", "rm") for command in runner.commands)
    assert not any(command[:3] == ("docker", "volume", "rm") for command in runner.commands)
    assert not any(command[:3] == ("docker", "network", "rm") for command in runner.commands)
    workspace = tmp_path / "restore-workspaces" / "restore-drill-tenant-a-failed"
    assert workspace.is_dir()
    secret_files = tuple((workspace / "secrets").rglob("*password"))
    assert secret_files
    assert all(path.stat().st_mode & 0o077 == 0 for path in secret_files)
    assert (workspace / "neo4j.dump").stat().st_mode & 0o777 == 0o600
    sanitize = (workspace / "sanitize-control.sql").read_text(encoding="utf-8")
    assert "UPDATE control.routing SET healthy = false" in sanitize
    assert "neo4j-tenant-a:7687" in sanitize

    runner.commands.clear()
    executor.cleanup("restore-drill-tenant-a-failed")

    assert not workspace.exists()
    assert runner.commands[-1] == (
        "docker",
        "network",
        "rm",
        "memory-restore-drill-tenant-a-failed",
    )


def test_restore_cleanup_rejects_missing_or_non_exact_target(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        RecordingRunner(),
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(RestoreSafetyError, match="identifier"):
        executor.cleanup("tenant-a")
    with pytest.raises(RestoreSafetyError, match="does not exist"):
        executor.cleanup("restore-drill-tenant-a-missing")


@pytest.mark.parametrize("failure_index", range(7))
def test_restore_cleanup_attempts_every_resource_and_is_retry_safe(
    tmp_path: Path,
    failure_index: int,
) -> None:
    class PartiallyFailingRunner(RecordingRunner):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def remove_docker_resource(self, kind: str, name: str) -> None:
            super().remove_docker_resource(kind, name)
            if len(self.commands) - 1 == failure_index and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("sensitive daemon response")

    runner = PartiallyFailingRunner()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )
    workspace = tmp_path / "restore-workspaces" / "restore-drill-partial-cleanup"
    workspace.mkdir()

    with pytest.raises(RestoreSafetyError, match="incomplete") as raised:
        executor.cleanup("restore-drill-partial-cleanup")

    assert "sensitive" not in str(raised.value)
    assert workspace.is_dir()
    assert len(runner.commands) == 7

    executor.cleanup("restore-drill-partial-cleanup")

    assert len(runner.commands) == 14
    assert not workspace.exists()


def test_restore_rejects_wrong_schema_before_creating_resources(tmp_path: Path) -> None:
    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    tenant_artifact = next(
        artifact for artifact in manifest.artifacts if artifact.store is BackupStore.TENANT_POSTGRES
    )
    incompatible = manifest.model_copy(
        update={
            "artifacts": tuple(
                artifact.model_copy(update={"schema_version": "old"})
                if artifact is tenant_artifact
                else artifact
                for artifact in manifest.artifacts
            )
        }
    )
    runner = RecordingRunner()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(RestoreSafetyError, match="schema"):
        executor.execute(
            manifest=incompatible,
            ciphertext_paths=ciphertext_paths,
            target_id="restore-drill-tenant-a-old",
            operator_id="operator-alice",
            age_identity_file=identity,
        )

    assert runner.commands == []


def test_restore_rejects_corrupt_ciphertext_before_creating_resources(tmp_path: Path) -> None:
    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    tenant_artifact = next(
        artifact for artifact in manifest.artifacts if artifact.store is BackupStore.TENANT_POSTGRES
    )
    ciphertext_paths[tenant_artifact.artifact_id].write_bytes(
        b"x" * tenant_artifact.ciphertext_bytes
    )
    runner = RecordingRunner()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(BackupValidationError, match="checksum"):
        executor.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext_paths,
            target_id="restore-drill-tenant-a-corrupt",
            operator_id="operator-alice",
            age_identity_file=identity,
        )

    assert runner.commands == []


def test_restore_wrong_age_identity_creates_no_docker_resources(tmp_path: Path) -> None:
    class WrongIdentityRunner(RecordingRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            if arguments[0] == "age":
                raise RuntimeError("wrong private key with sensitive detail")
            super().run(arguments, environment=environment)

    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "wrong-identity.txt", "AGE-SECRET-KEY-WRONG")
    runner = WrongIdentityRunner()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(EncryptionCommandFailed, match="decryption") as raised:
        executor.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext_paths,
            target_id="restore-drill-tenant-a-wrong-key",
            operator_id="operator-alice",
            age_identity_file=identity,
        )

    assert "sensitive" not in str(raised.value)
    assert not any(command[0] == "docker" for command in runner.commands)


def test_restore_command_failure_preserves_workspace_and_created_resources(
    tmp_path: Path,
) -> None:
    class FailingRunner(RecordingRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            super().run(arguments, environment=environment)
            if tuple(arguments[:3]) == ("docker", "volume", "create"):
                raise RuntimeError("volume creation failed")

    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    runner = FailingRunner()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="volume creation"):
        executor.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext_paths,
            target_id="restore-drill-command-failure",
            operator_id="operator-alice",
            age_identity_file=identity,
        )

    workspace = tmp_path / "restore-workspaces" / "restore-drill-command-failure"
    assert workspace.is_dir()
    assert not any(command[:2] == ("docker", "rm") for command in runner.commands)
    assert not any(command[:3] == ("docker", "network", "rm") for command in runner.commands)


def test_failed_neo4j_load_restores_private_dump_permissions(tmp_path: Path) -> None:
    class FailingLoader(RecordingRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            super().run(arguments, environment=environment)
            if "database" in arguments and "load" in arguments:
                raise RuntimeError("neo4j load failed")

    manifest, ciphertext_paths = _manifest(tmp_path)
    identity = _protected_file(tmp_path / "identity.txt", "AGE-SECRET-KEY-1")
    runner = FailingLoader()
    executor = _executor(
        tmp_path,
        runner,
        FixedVerifier(
            RestoreVerificationReport(
                public_recall=True,
                private_memory=True,
                governance=True,
                agent_runs=True,
                tenant_isolation=True,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="neo4j load failed"):
        executor.execute(
            manifest=manifest,
            ciphertext_paths=ciphertext_paths,
            target_id="restore-drill-neo4j-load-failure",
            operator_id="operator-alice",
            age_identity_file=identity,
        )

    dump = tmp_path / "restore-workspaces" / "restore-drill-neo4j-load-failure" / "neo4j.dump"
    assert dump.stat().st_mode & 0o777 == 0o600


def test_docker_verifier_runs_inside_internal_network_without_secrets_in_argv(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    secrets = workspace / "secrets"
    tenant_secrets = secrets / "tenants" / "tenant-a"
    tenant_secrets.mkdir(parents=True)
    _protected_file(secrets / "postgres_superuser_password", "super-secret")
    _protected_file(tenant_secrets / "postgres_password", "tenant-secret")
    _protected_file(tenant_secrets / "neo4j_password", "neo-secret")
    report = RestoreVerificationReport(
        public_recall=True,
        private_memory=True,
        governance=True,
        agent_runs=True,
        tenant_isolation=True,
    )
    runner = RecordingRunner(verification_report=report)
    verifier = DockerCanonicalRestoreVerifier(
        runner=runner,
        image="coengram:0.1.0",
    )
    context = RestoreVerificationContext(
        target_id="restore-drill-tenant-a-verify",
        source_tenant_id="tenant-a",
        network_name="memory-restore-drill-tenant-a-verify",
        workspace=workspace,
        secrets_directory=secrets,
        control_database_name="memory_control",
        control_database_user="postgres",
        postgres_host="postgres",
        postgres_port=5432,
        embedding_model="BAAI/bge-small-en-v1.5",
        expectations=_expectations(),
    )

    assert verifier.verify(context) == report
    command = runner.commands[0]
    assert command[:2] == ("docker", "run")
    assert "--rm" not in command
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert command[command.index("--network") + 1] == context.network_name
    assert f"RESTORE_TARGET_ID={context.target_id}" in command
    assert "RESTORE_EXPECTATIONS_PATH=/run/restore/restore-expectations.json" in command
    assert "--publish" not in command
    command_text = " ".join(command)
    assert "super-secret" not in command_text
    assert "tenant-secret" not in command_text
    assert "neo-secret" not in command_text
    expectations_file = workspace / "restore-expectations.json"
    assert expectations_file.stat().st_mode & 0o077 == 0
    assert KNOWLEDGE_CLAIM not in expectations_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restore_checks_report_each_canonical_probe_without_content() -> None:
    class Probe:
        async def verify_public_recall(self) -> bool:
            return True

        async def verify_private_memory(self) -> bool:
            return True

        async def verify_governance(self) -> bool:
            raise RuntimeError("sensitive row value")

        async def verify_agent_runs(self) -> bool:
            return True

        async def verify_no_other_tenant_routing(self) -> bool:
            return True

    report = await run_restore_checks(Probe())

    assert report == RestoreVerificationReport(
        public_recall=True,
        private_memory=True,
        governance=False,
        agent_runs=True,
        tenant_isolation=True,
    )
    assert "sensitive" not in report.model_dump_json()


class _CloseableRouter:
    def __init__(self, delegate: object | None = None) -> None:
        self._delegate = delegate

    def for_tenant(self, tenant_id: str) -> object:
        if self._delegate is None:
            raise AssertionError("This test router has no Tenant Memory delegate")
        return self._delegate.for_tenant(tenant_id)  # type: ignore[attr-defined]

    async def close(self) -> None:
        return None


def _canonical_probe(
    target_id: str,
    expectations: RestoreExpectations | None = None,
) -> CanonicalRestoreProbe:
    return CanonicalRestoreProbe(
        target_id=target_id,
        source_tenant_id="tenant-a",
        control_database_url="postgresql://unused",
        tenant_secrets_directory=Path("/unused"),
        postgres_host="postgres",
        postgres_port=5432,
        embedding_model="test-model",
        expectations=expectations or _expectations(),
    )


@pytest.mark.asyncio
async def test_public_recall_probe_issues_token_and_uses_typed_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    memory_router = InMemoryTenantMemoryRouter(("tenant-a",))
    await memory_router.for_tenant("tenant-a").publish_tenant_knowledge(
        "candidate-a",
        KNOWLEDGE_CLAIM,
        0.9,
        "agent-a",
    )
    memory = MemoryModule(memory_router)
    router = _CloseableRouter(memory_router)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (control, tokens, router, object(), memory),
    )
    probe = _canonical_probe("restore-drill-http-success")

    assert await probe.verify_public_recall() is True
    verifier = store.get_principal("restore-verifier-restore-drill-http-success")
    assert verifier is not None and verifier.active is False


@pytest.mark.asyncio
async def test_public_recall_probe_verifies_more_than_one_page_via_graph_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    memory_router = InMemoryTenantMemoryRouter(("tenant-a",))
    tenant_store = memory_router.for_tenant("tenant-a")
    for index in range(101):
        await tenant_store.publish_tenant_knowledge(
            f"candidate-{index:03d}",
            f"restore marker knowledge {index:03d}",
            0.9,
            "agent-a",
        )
    knowledge = await tenant_store.list_tenant_knowledge()
    digests = tuple(
        sorted(
            tenant_knowledge_digest(
                content=item.content,
                confidence=item.confidence,
                actor_id=item.provenance.actor_id,
                source=item.provenance.source,
            )
            for item in knowledge
        )
    )
    expectations = _expectations().model_copy(update={"tenant_knowledge_digests": digests})
    router = _CloseableRouter(memory_router)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (
            control,
            tokens,
            router,
            object(),
            MemoryModule(memory_router),
        ),
    )

    assert (
        await _canonical_probe(
            "restore-drill-http-many-knowledge",
            expectations,
        ).verify_public_recall()
        is True
    )


@pytest.mark.asyncio
async def test_public_recall_probe_rejects_empty_restored_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    memory_router = InMemoryTenantMemoryRouter(("tenant-a",))
    memory = MemoryModule(memory_router)
    router = _CloseableRouter(memory_router)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (control, tokens, router, object(), memory),
    )

    assert await _canonical_probe("restore-drill-http-empty").verify_public_recall() is False


@pytest.mark.asyncio
async def test_public_recall_probe_requires_complete_expected_digest_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    memory_router = InMemoryTenantMemoryRouter(("tenant-a",))
    await memory_router.for_tenant("tenant-a").publish_tenant_knowledge(
        "candidate-a",
        KNOWLEDGE_CLAIM,
        0.9,
        "agent-a",
    )
    missing_claim = "content that must never appear in restore diagnostics"
    missing_digest = tenant_knowledge_digest(
        content=missing_claim,
        confidence=0.8,
        actor_id="agent-b",
        source="candidate:candidate-b",
    )
    expectations = _expectations().model_copy(
        update={
            "tenant_knowledge_digests": (
                *_expectations().tenant_knowledge_digests,
                missing_digest,
            )
        }
    )
    router = _CloseableRouter(memory_router)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (control, tokens, router, object(), MemoryModule(memory_router)),
    )
    caplog.set_level(logging.WARNING, logger="memory.restore")

    assert (
        await _canonical_probe(
            "restore-drill-http-incomplete",
            expectations,
        ).verify_public_recall()
        is False
    )

    diagnostic = vars(caplog.records[-1])
    assert diagnostic["expected_digest_count"] == 2
    assert diagnostic["actual_digest_count"] == 1
    assert diagnostic["missing_digest_count"] == 1
    assert diagnostic["unexpected_digest_count"] == 0
    assert missing_claim not in caplog.text
    assert missing_digest not in caplog.text


@pytest.mark.asyncio
async def test_public_recall_probe_rejects_dependency_failure_as_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableRouter:
        def for_tenant(self, _tenant_id: str) -> object:
            raise TenantMemoryUnavailable("sensitive dependency detail")

    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    memory = MemoryModule(UnavailableRouter())  # type: ignore[arg-type]
    graph_router = InMemoryTenantMemoryRouter(("tenant-a",))
    await graph_router.for_tenant("tenant-a").publish_tenant_knowledge(
        "candidate-a",
        KNOWLEDGE_CLAIM,
        0.9,
        "agent-a",
    )
    router = _CloseableRouter(graph_router)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (control, tokens, router, object(), memory),
    )

    assert await _canonical_probe("restore-drill-http-failure").verify_public_recall() is False


@pytest.mark.asyncio
async def test_private_memory_probe_authenticates_and_proves_lifecycle_and_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = "user-alice"
    original = PrivateMemoryInspection(
        id="private-original",
        owner_principal_id=owner_id,
        state=MemoryState.SUPERSEDED,
        operation_id="operation-original",
        mutation_state=MutationState.APPLIED,
        content="Sensitive original preference",
        kind=MemoryKind.PREFERENCE,
        confidence=0.8,
        created_at=STARTED,
    )
    corrected = PrivateMemoryInspection(
        id="private-corrected",
        owner_principal_id=owner_id,
        state=MemoryState.ACTIVE,
        operation_id="operation-corrected",
        mutation_state=MutationState.APPLIED,
        content="Sensitive corrected preference",
        kind=MemoryKind.PREFERENCE,
        confidence=0.95,
        created_at=COMPLETED,
        supersedes_id=original.id,
    )
    erased = PrivateMemoryInspection(
        id="private-erased",
        owner_principal_id=owner_id,
        state=MemoryState.ERASED,
        operation_id="operation-erased",
        mutation_state=MutationState.APPLIED,
        created_at=STARTED,
    )
    tombstone = ErasureTombstone(
        id="erasure-completed",
        memory_id=erased.id,
        requester_id=owner_id,
        owner_principal_id=owner_id,
        created_at=STARTED,
        reviewed_by="admin-a",
        completed_at=COMPLETED,
    )
    original_item = MemoryItem(
        id=original.id,
        owner_principal_id=owner_id,
        scope=MemoryScope.PRIVATE,
        content=original.content or "",
        kind=MemoryKind.PREFERENCE,
        confidence=0.8,
        provenance=Provenance(actor_id=owner_id, source="retain"),
        created_at=STARTED,
        state=MemoryState.SUPERSEDED,
    )
    corrected_item = MemoryItem(
        id=corrected.id,
        owner_principal_id=owner_id,
        scope=MemoryScope.PRIVATE,
        content=corrected.content or "",
        kind=MemoryKind.PREFERENCE,
        confidence=0.95,
        provenance=Provenance(actor_id=owner_id, source="correction"),
        created_at=COMPLETED,
        supersedes_id=original.id,
    )
    inspections = (original, corrected, erased)
    archive = encode_archive(
        ArchiveScope.PRIVATE,
        [original_item.model_dump(mode="json"), corrected_item.model_dump(mode="json")],
        erasure_records=[tombstone.model_dump(mode="json")],
    )
    authenticated_sessions: list[TenantSession] = []

    class PersonalMemory:
        async def inspect_private(
            self,
            session: TenantSession,
        ) -> tuple[PrivateMemoryInspection, ...]:
            authenticated_sessions.append(session)
            return inspections

        async def export_private(self, session: TenantSession) -> bytes:
            authenticated_sessions.append(session)
            return archive

        async def recall(self, session: TenantSession, _query: object) -> RecallResult:
            authenticated_sessions.append(session)
            return RecallResult(items=(corrected_item,))

    def target(digests: Sequence[str]) -> RestoreProofTarget:
        ordered = tuple(sorted(digests))
        return RestoreProofTarget(
            count=len(ordered),
            digest=content_safe_digest(ordered),
            representative_digest=ordered[0],
        )

    expected_private = PrivateMemoryRestoreProof(
        active=target((private_memory_item_digest(corrected),)),
        correction_chain=target((private_memory_correction_digest(original, corrected),)),
        completed_erasure=target((private_memory_erasure_digest(erased, tombstone),)),
    )
    store = InMemoryControlStore()
    tokens = TokenService(store)
    control = ControlModule(store, tokens)
    control.create_tenant("tenant-a", "Tenant A")
    control.create_principal(owner_id, "Alice", PrincipalKind.USER.value)
    control.grant_membership("tenant-a", owner_id, "tenant_member")

    class PersonalGraphStore:
        def __init__(self) -> None:
            self.items: tuple[MemoryItem, ...] = (original_item, corrected_item)

        async def list_private(self, requested_owner_id: str) -> tuple[MemoryItem, ...]:
            assert requested_owner_id == owner_id
            return self.items

    class PersonalRouter(_CloseableRouter):
        def __init__(self) -> None:
            self.store = PersonalGraphStore()

        def for_tenant(self, tenant_id: str) -> PersonalGraphStore:
            assert tenant_id == "tenant-a"
            return self.store

    router = PersonalRouter()
    expectations = _expectations().model_copy(update={"private_memory": expected_private})
    probe = _canonical_probe("restore-drill-private-proof", expectations)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_private_memory_owner_ids",
        lambda _self, _database_url: (owner_id,),
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (control, tokens, router, object(), PersonalMemory()),
    )

    assert await probe.verify_private_memory() is True
    assert authenticated_sessions
    assert all(session.actor_id == owner_id for session in authenticated_sessions)

    mismatched = expectations.model_copy(
        update={
            "private_memory": expected_private.model_copy(
                update={
                    "completed_erasure": expected_private.completed_erasure.model_copy(
                        update={"digest": content_safe_digest(("missing-tombstone",))}
                    )
                }
            )
        }
    )
    assert (
        await _canonical_probe(
            "restore-drill-private-mismatch",
            mismatched,
        ).verify_private_memory()
        is False
    )

    control.disable_principal(owner_id)
    assert await probe.verify_private_memory() is True

    router.store.items = (
        original_item,
        corrected_item.model_copy(update={"content": "Corrupted restored graph content"}),
    )
    assert await probe.verify_private_memory() is False
    router.store.items = (original_item, corrected_item)

    router.store.items = (original_item, corrected_item, corrected_item)
    assert await probe.verify_private_memory() is False
    router.store.items = (original_item, corrected_item)

    resurrected = MemoryItem(
        id=erased.id,
        owner_principal_id=owner_id,
        scope=MemoryScope.PRIVATE,
        content="Sensitive erased content resurrected in graph",
        kind=MemoryKind.EXPLICIT,
        confidence=1.0,
        provenance=Provenance(actor_id=owner_id, source="retain"),
        created_at=STARTED,
    )
    router.store.items = (*router.store.items, resurrected)
    assert await probe.verify_private_memory() is False


@pytest.mark.asyncio
async def test_governance_probe_rejects_missing_expected_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyGovernanceMemory:
        async def list_knowledge_candidates(self, _session: object) -> tuple[object, ...]:
            return ()

    router = _CloseableRouter()
    expectations = _expectations().model_copy(
        update={
            "governance_candidate_count": 1,
            "governance_digest": content_safe_digest([{"id": "expected-candidate"}]),
        }
    )
    base = _canonical_probe("restore-drill-governance-missing")
    probe = CanonicalRestoreProbe(
        target_id=base.target_id,
        source_tenant_id=base.source_tenant_id,
        control_database_url=base.control_database_url,
        tenant_secrets_directory=base.tenant_secrets_directory,
        postgres_host=base.postgres_host,
        postgres_port=base.postgres_port,
        embedding_model=base.embedding_model,
        expectations=expectations,
    )
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (object(), object(), router, object(), EmptyGovernanceMemory()),
    )

    assert await probe.verify_governance() is False


@pytest.mark.asyncio
async def test_agent_run_probe_rejects_missing_expected_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRunRepository:
        def __init__(self, _database_url: str) -> None:
            pass

        def get(self, _tenant_id: str, _run_id: str) -> None:
            return None

    router = _CloseableRouter()
    expectations = _expectations().model_copy(
        update={
            "agent_run_count": 1,
            "representative_agent_run_id": "run-expected",
            "representative_agent_run_digest": content_safe_digest({"run": "expected"}),
            "representative_activegraph_event_count": 1,
            "representative_activegraph_event_digest": content_safe_digest(
                {"activegraph": "expected"}
            ),
        }
    )
    base = _canonical_probe("restore-drill-agent-run-missing")
    probe = CanonicalRestoreProbe(
        target_id=base.target_id,
        source_tenant_id=base.source_tenant_id,
        control_database_url=base.control_database_url,
        tenant_secrets_directory=base.tenant_secrets_directory,
        postgres_host=base.postgres_host,
        postgres_port=base.postgres_port,
        embedding_model=base.embedding_model,
        expectations=expectations,
    )
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(CanonicalRestoreProbe, "_tenant_row_count", lambda *_args: 1)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (object(), object(), router, object(), object()),
    )
    monkeypatch.setattr(
        "agent_memory_service.restore_verify.PostgresAgentRunRepository",
        MissingRunRepository,
    )

    assert await probe.verify_agent_runs() is False


@pytest.mark.asyncio
async def test_agent_run_probe_verifies_native_activegraph_replay_and_fails_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AgentModelSettings(provider="recorded", adapter="recorded")
    budget = AgentBudget()
    queued = AgentRunEvent(
        sequence=1,
        type="agent.run.queued",
        data={
            "capability": "restore-capability",
            "provider": settings.provider,
            "adapter": settings.adapter,
            "model": settings.model,
            "settings": settings.model_dump(mode="json"),
            "budget": budget.model_dump(mode="json"),
        },
        created_at=STARTED,
    )
    snapshot = AgentRunSnapshot(
        run_id="run-activegraph",
        tenant_id="tenant-a",
        actor_id="agent-a",
        actor_kind=PrincipalKind.AGENT,
        roles=("tenant_member",),
        invocation=AgentInvocation(
            capability="restore-capability",
            input={},
            idempotency_key="restore-run",
        ),
        command={},
        settings=settings,
        budget=budget,
        state=AgentRunState.QUEUED,
        model_calls=0,
        tool_calls=0,
        cost_usd=Decimal("0"),
        events=(queued,),
    )
    native_event = Event(
        id="native-run-activegraph-1",
        type=queued.type,
        payload={
            "agent_memory_run_event": {
                "version": 1,
                "data": queued.data,
                "created_at": queued.created_at.isoformat(),
            }
        },
        actor=snapshot.actor_id,
        frame_id="frame-run-activegraph",
        timestamp=queued.created_at.isoformat(),
    )

    class Repository:
        def __init__(self, _database_url: str) -> None:
            pass

        def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None:
            return snapshot if (tenant_id, run_id) == ("tenant-a", snapshot.run_id) else None

    class NativeStore:
        event = native_event

        def __init__(self, _database_url: str, run_id: str) -> None:
            self.run_id = run_id

        def iter_events(self) -> object:
            return iter((self.event,))

        def get_run(self) -> object:
            return object()

        def get_event(self, event_id: str) -> Event | None:
            return self.event if event_id == self.event.id else None

        def append(self, _event: Event) -> None:
            raise AssertionError("An exact restore projection must not append")

        def close(self) -> None:
            return None

    expectations = _expectations().model_copy(
        update={
            "agent_run_count": 1,
            "representative_agent_run_id": snapshot.run_id,
            "representative_agent_run_digest": content_safe_digest(
                snapshot.model_dump(mode="json")
            ),
            "representative_activegraph_event_count": 1,
            "representative_activegraph_event_digest": activegraph_event_sequence_digest(
                (native_event,)
            ),
        }
    )
    router = _CloseableRouter()
    probe = _canonical_probe("restore-drill-agent-native", expectations)
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_require_tenant_schema",
        lambda _self, _governance: "postgresql://unused",
    )
    monkeypatch.setattr(CanonicalRestoreProbe, "_tenant_row_count", lambda *_args: 1)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (object(), object(), router, object(), object()),
    )
    monkeypatch.setattr(
        "agent_memory_service.restore_verify.PostgresAgentRunRepository",
        Repository,
    )
    monkeypatch.setattr(
        "agent_memory_service.restore_verify.PostgresEventStore",
        NativeStore,
    )

    assert await probe.verify_agent_runs() is True

    NativeStore.event = Event(
        id=native_event.id,
        type=native_event.type,
        payload={**native_event.payload, "unexpected": True},
        actor=native_event.actor,
        frame_id=native_event.frame_id,
        timestamp=native_event.timestamp,
    )
    assert await probe.verify_agent_runs() is False


@pytest.mark.asyncio
async def test_tenant_isolation_probe_rejects_a_restored_backup_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Control:
        def list_tenant_routes(self) -> tuple[TenantRouteRecord, ...]:
            return (
                TenantRouteRecord(
                    tenant_id="tenant-a",
                    neo4j_service_address="neo4j-tenant-a:7687",
                    neo4j_secret_name="tenant-a/neo4j_password",
                    tenant_database_name="tenant_tenant_a",
                    tenant_database_role="tenant_tenant_a_rw",
                    healthy=True,
                ),
            )

    router = _CloseableRouter()
    probe = _canonical_probe("restore-drill-barrier-proof")
    monkeypatch.setattr(CanonicalRestoreProbe, "_require_control_schema", lambda _self: None)
    monkeypatch.setattr(
        CanonicalRestoreProbe,
        "_modules",
        lambda _self: (Control(), object(), router, object(), object()),
    )
    monkeypatch.setattr(CanonicalRestoreProbe, "_backup_barrier_count", lambda _self: 1)

    assert await probe.verify_no_other_tenant_routing() is False

    monkeypatch.setattr(CanonicalRestoreProbe, "_backup_barrier_count", lambda _self: 0)
    assert await probe.verify_no_other_tenant_routing() is True
