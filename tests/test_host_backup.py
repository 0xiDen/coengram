from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
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
from agent_memory_service.auth import TokenService
from agent_memory_service.backup import (
    BackupBarrierRecord,
    BackupPlan,
    BackupValidationError,
    PrivateMemoryRestoreProof,
    RestoreExpectations,
    RestoreProofTarget,
    RestoreSafetyError,
    RetentionPin,
    RetentionProtectionPort,
)
from agent_memory_service.cli import run_cli
from agent_memory_service.control import ControlModule, InMemoryControlStore
from agent_memory_service.governance import CandidateStatus, KnowledgeCandidate
from agent_memory_service.host_backup import (
    HostBackupConfig,
    HostBackupOrchestrator,
    LocalBackupArtifactPublisher,
    PostgresBackupExpectationCollector,
    PostgresBackupSource,
)
from agent_memory_service.lifecycle import ErasureTombstone
from agent_memory_service.models import (
    MemoryKind,
    MemoryState,
    MutationState,
    PrincipalKind,
    PrivateMemoryInspection,
)
from agent_memory_service.pseudonyms import TelemetryPseudonymizer

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
_TELEMETRY = TelemetryPseudonymizer(b"0123456789abcdef0123456789abcdef")


class RecordingBackupRunner:
    def __init__(self, *, fail_neo4j_archive: bool = False) -> None:
        self.fail_neo4j_archive = fail_neo4j_archive
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str] | None] = []
        self.neo4j_handoff_modes: list[int] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        command = tuple(arguments)
        self.commands.append(command)
        self.environments.append(environment)
        if command[0] == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"postgres-dump")
        elif command[:2] == ("docker", "run"):
            if self.fail_neo4j_archive:
                raise RuntimeError("archive failed")
            mount = command[command.index("--volume", command.index("--volume") + 1) + 1]
            host_directory = Path(mount.removesuffix(":/backups"))
            self.neo4j_handoff_modes.append(stat.S_IMODE(host_directory.stat().st_mode))
            (host_directory / "neo4j.dump").write_bytes(b"neo4j-dump")
        elif command[0] == "age" and "--encrypt" in command:
            source = Path(command[-1])
            destination = Path(command[command.index("--output") + 1])
            destination.write_bytes(b"age:" + source.read_bytes())


class FixedExpectationCollector:
    def __init__(self, runner: RecordingBackupRunner) -> None:
        self._runner = runner
        self.command_counts_at_collection: list[int] = []

    def collect(self, _plan: object) -> RestoreExpectations:
        self.command_counts_at_collection.append(len(self._runner.commands))
        return RestoreExpectations(
            tenant_knowledge_digests=(hashlib.sha256(b"tenant-knowledge").hexdigest(),),
            governance_candidate_count=0,
            governance_digest=hashlib.sha256(b"[]").hexdigest(),
            agent_run_count=0,
            private_memory=PrivateMemoryRestoreProof(
                active=RestoreProofTarget(
                    count=1,
                    digest=hashlib.sha256(b"active").hexdigest(),
                    representative_digest=hashlib.sha256(b"active-rep").hexdigest(),
                ),
                correction_chain=RestoreProofTarget(
                    count=1,
                    digest=hashlib.sha256(b"correction").hexdigest(),
                    representative_digest=hashlib.sha256(b"correction-rep").hexdigest(),
                ),
                completed_erasure=RestoreProofTarget(
                    count=1,
                    digest=hashlib.sha256(b"erasure").hexdigest(),
                    representative_digest=hashlib.sha256(b"erasure-rep").hexdigest(),
                ),
            ),
        )


class RecordingConsistencyBarrier:
    def __init__(
        self,
        *,
        fail_verification: bool = False,
        runner: RecordingBackupRunner | None = None,
    ) -> None:
        self.fail_verification = fail_verification
        self.runner = runner
        self.calls: list[tuple[str, str]] = []
        self.record: BackupBarrierRecord | None = None
        self.age_seen_at_exit: bool | None = None

    def enter(self, plan: BackupPlan) -> None:
        self.calls.append(("enter", plan.tenant_id))

    def verify_quiescent(self, plan: BackupPlan) -> None:
        self.calls.append(("verify", plan.tenant_id))
        if self.fail_verification:
            raise BackupValidationError("Tenant projections are pending")

    def exit(self, plan: BackupPlan) -> None:
        self.calls.append(("exit", plan.tenant_id))
        if self.runner is not None:
            self.age_seen_at_exit = any(command[0] == "age" for command in self.runner.commands)

    def inspect(self, tenant_id: str) -> BackupBarrierRecord | None:
        self.calls.append(("inspect", tenant_id))
        return self.record

    def recover(self, tenant_id: str, barrier_id: str) -> None:
        self.calls.append(("recover", f"{tenant_id}:{barrier_id}"))


def _protected_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _control() -> ControlModule:
    store = InMemoryControlStore()
    control = ControlModule(store, TokenService(store))
    control.create_tenant("tenant-a", "Product A")
    control.register_tenant_route(
        "tenant-a",
        neo4j_service_address="neo4j-tenant-a:7687",
        neo4j_secret_name="tenant-a/neo4j_password",
        tenant_database_name="tenant_tenant_a",
        tenant_database_role="tenant_tenant_a_rw",
        healthy=True,
    )
    return control


def _operator(
    tmp_path: Path,
    runner: RecordingBackupRunner,
    *,
    replacements: list[Path] | None = None,
    retention_protection: RetentionProtectionPort | None = None,
    expectation_collector: FixedExpectationCollector | None = None,
    consistency_barrier: RecordingConsistencyBarrier | None = None,
) -> HostBackupOrchestrator:
    staging = tmp_path / "staging"
    artifacts = tmp_path / "artifacts"
    evidence = tmp_path / "evidence"
    staging.mkdir()
    artifacts.mkdir()
    evidence.mkdir()
    compose_file = tmp_path / "tenant.compose.yaml"
    compose_file.write_text("services: {neo4j: {}}\n", encoding="utf-8")
    recipients = _protected_file(tmp_path / "credentials" / "recipients.txt", "age1public")
    control_password = _protected_file(
        tmp_path / "credentials" / "control-postgres-password",
        "control-secret",
    )
    tenant_password = _protected_file(
        tmp_path / "tenant-secrets" / "tenant-a" / "postgres_password",
        "tenant-secret",
    )
    tenant_password.chmod(0o640)

    def replace(source: Path, destination: Path) -> None:
        if replacements is not None:
            replacements.append(destination)
        os.replace(source, destination)

    return HostBackupOrchestrator(
        control=_control(),
        runner=runner,
        publisher=LocalBackupArtifactPublisher(artifacts, replace=replace),
        config=HostBackupConfig(
            staging_directory=staging,
            artifact_directory=artifacts,
            tenant_compose_file=compose_file,
            tenant_secrets_directory=tmp_path / "tenant-secrets",
            age_recipients_file=recipients,
            age_key_id="backup-key-1",
            control_postgres=PostgresBackupSource(
                host="postgres.internal",
                port=5432,
                database="memory_control",
                user="memory_control",
                password_file=control_password,
            ),
            postgres_store_version="17.6",
            control_schema_version="0002_channel_bindings",
            tenant_schema_version="0002_candidate_memory_findings",
            neo4j_store_version="5.26.28-community",
            neo4j_schema_version="1",
            evidence_directory=evidence,
        ),
        clock=lambda: NOW,
        retention_protection=retention_protection,
        expectation_collector=expectation_collector or FixedExpectationCollector(runner),
        consistency_barrier=consistency_barrier or RecordingConsistencyBarrier(),
        telemetry_pseudonymizer=_TELEMETRY,
    )


class RetentionProtection:
    def __init__(self, responses: list[tuple[RetentionPin, ...]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, datetime]] = []

    def pins_for_tenant(
        self,
        tenant_id: str,
        *,
        now: datetime,
    ) -> tuple[RetentionPin, ...]:
        self.calls.append((tenant_id, now))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def test_backup_plan_is_route_exact_and_has_no_host_side_effects(tmp_path: Path) -> None:
    runner = RecordingBackupRunner()
    operator = _operator(tmp_path, runner)

    plan = operator.plan("tenant-a", backup_id="backup-tenant-a-001")

    assert plan.database_name == "tenant_tenant_a"
    assert plan.database_role == "tenant_tenant_a_rw"
    assert plan.neo4j_service_name == "neo4j-tenant-a"
    assert {artifact.store.value for artifact in plan.artifacts} == {
        "control-postgres",
        "tenant-postgres",
        "tenant-neo4j",
    }
    assert runner.commands == []


def test_backup_plan_rejects_path_like_tenant_identifier_before_host_commands(
    tmp_path: Path,
) -> None:
    runner = RecordingBackupRunner()
    operator = _operator(tmp_path, runner)

    with pytest.raises(BackupValidationError, match="Tenant identifier"):
        operator.plan("../tenant-a", backup_id="backup-tenant-a-001")
    assert runner.commands == []


def test_restore_expectations_are_collected_before_each_backup_command_sequence(
    tmp_path: Path,
) -> None:
    runner = RecordingBackupRunner()
    collector = FixedExpectationCollector(runner)
    operator = _operator(tmp_path, runner, expectation_collector=collector)

    operator.create("tenant-a", backup_id="backup-expectation-order-a")
    commands_after_first = len(runner.commands)
    operator.create("tenant-a", backup_id="backup-expectation-order-b")

    assert collector.command_counts_at_collection == [0, commands_after_first]
    assert len(runner.commands) > commands_after_first


def test_backup_holds_consistency_barrier_across_every_store_capture(tmp_path: Path) -> None:
    runner = RecordingBackupRunner()
    barrier = RecordingConsistencyBarrier(runner=runner)
    operator = _operator(tmp_path, runner, consistency_barrier=barrier)

    operator.create("tenant-a", backup_id="backup-consistent-cut")

    assert barrier.calls == [
        ("enter", "tenant-a"),
        ("verify", "tenant-a"),
        ("exit", "tenant-a"),
    ]
    assert barrier.age_seen_at_exit is False
    assert any(command[0] == "age" for command in runner.commands)
    assert len(runner.commands) > 0


def test_failed_consistency_check_aborts_before_any_backup_command(tmp_path: Path) -> None:
    runner = RecordingBackupRunner()
    barrier = RecordingConsistencyBarrier(fail_verification=True)
    operator = _operator(tmp_path, runner, consistency_barrier=barrier)

    with pytest.raises(BackupValidationError, match="projections are pending"):
        operator.create("tenant-a", backup_id="backup-incoherent-cut")

    assert barrier.calls == [
        ("enter", "tenant-a"),
        ("verify", "tenant-a"),
        ("exit", "tenant-a"),
    ]
    assert runner.commands == []
    assert not (tmp_path / "artifacts" / "backup-incoherent-cut").exists()


def test_postgres_expectation_collector_hashes_published_knowledge_and_governance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingBackupRunner()
    operator = _operator(tmp_path, runner)
    plan = operator.plan("tenant-a", backup_id="backup-expectation-production")
    claim = "Sensitive Product A rollback knowledge"
    candidate = KnowledgeCandidate(
        id="candidate-a",
        tenant_id="tenant-a",
        claim=claim,
        confidence=0.9,
        proposer_id="agent-a",
        source_memory_ids=("private-memory-a",),
        status=CandidateStatus.PUBLISHED,
        created_at=NOW,
    )
    original_content = "Sensitive original Private Memory"
    corrected_content = "Sensitive corrected Private Memory"
    inspections = (
        PrivateMemoryInspection(
            id="private-original",
            owner_principal_id="user-alice",
            state=MemoryState.SUPERSEDED,
            operation_id="operation-original",
            mutation_state=MutationState.APPLIED,
            content=original_content,
            kind=MemoryKind.EXPLICIT,
            confidence=0.8,
            created_at=NOW,
        ),
        PrivateMemoryInspection(
            id="private-corrected",
            owner_principal_id="user-alice",
            state=MemoryState.ACTIVE,
            operation_id="operation-corrected",
            mutation_state=MutationState.APPLIED,
            content=corrected_content,
            kind=MemoryKind.EXPLICIT,
            confidence=0.9,
            created_at=NOW,
            supersedes_id="private-original",
        ),
        PrivateMemoryInspection(
            id="private-erased",
            owner_principal_id="user-alice",
            state=MemoryState.ERASED,
            operation_id="operation-erased",
            mutation_state=MutationState.APPLIED,
            created_at=NOW,
        ),
    )
    tombstone = ErasureTombstone(
        id="erasure-a",
        memory_id="private-erased",
        requester_id="user-alice",
        owner_principal_id="user-alice",
        created_at=NOW,
        reviewed_by="admin-a",
        completed_at=NOW,
    )
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
        created_at=NOW,
    )
    snapshot = AgentRunSnapshot(
        run_id="run-a",
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
        id="native-run-a-1",
        type=queued.type,
        payload={
            "agent_memory_run_event": {
                "version": 1,
                "data": queued.data,
                "created_at": queued.created_at.isoformat(),
            }
        },
        actor="agent-a",
        frame_id="frame-run-a",
        timestamp=queued.created_at.isoformat(),
    )

    class Governance:
        def __init__(self, _database_url: str) -> None:
            pass

        async def list_candidates(self, _tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
            return (candidate,)

        async def list_private_memory_state(
            self,
            _tenant_id: str,
            _owner_principal_id: str,
        ) -> tuple[PrivateMemoryInspection, ...]:
            return inspections

        async def list_completed(
            self,
            _tenant_id: str,
            _owner_principal_id: str,
        ) -> tuple[ErasureTombstone, ...]:
            return (tombstone,)

    class Result:
        def __init__(
            self,
            row: tuple[object, ...] | None,
            rows: tuple[tuple[object, ...], ...] = (),
        ) -> None:
            self._row = row
            self._rows = rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

        def fetchall(self) -> tuple[tuple[object, ...], ...]:
            return self._rows

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, _parameters: object) -> Result:
            if "DISTINCT owner_principal_id" in statement:
                return Result(None, (("user-alice",),))
            if "count(*)" in statement:
                return Result((1,))
            if "SELECT run_id" in statement:
                return Result((snapshot.run_id,))
            return Result(None)

    class Repository:
        def __init__(self, _database_url: str) -> None:
            pass

        def get(self, tenant_id: str, run_id: str) -> AgentRunSnapshot | None:
            return snapshot if (tenant_id, run_id) == ("tenant-a", snapshot.run_id) else None

    class NativeStore:
        def __init__(self, _database_url: str, run_id: str) -> None:
            assert run_id == snapshot.run_id

        def iter_events(self) -> object:
            return iter((native_event,))

        def get_run(self) -> object:
            return object()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "agent_memory_service.host_backup.PostgresGovernanceStore",
        Governance,
    )
    monkeypatch.setattr(
        "agent_memory_service.host_backup.psycopg.connect",
        lambda _database_url: Connection(),
    )
    monkeypatch.setattr(
        "agent_memory_service.host_backup.PostgresAgentRunRepository",
        Repository,
    )
    monkeypatch.setattr(
        "agent_memory_service.host_backup.PostgresEventStore",
        NativeStore,
    )
    collector = PostgresBackupExpectationCollector(
        tmp_path / "tenant-secrets",
        postgres_host="postgres.internal",
        postgres_port=5432,
    )

    expectations = collector.collect(plan)

    assert len(expectations.tenant_knowledge_digests) == 1
    assert expectations.governance_candidate_count == 1
    assert expectations.agent_run_count == 1
    assert expectations.representative_activegraph_event_count == 1
    serialized = expectations.model_dump_json()
    assert expectations.private_memory.active.count == 1
    assert claim not in serialized
    assert original_content not in serialized
    assert corrected_content not in serialized
    assert "user-alice" not in serialized


def test_postgres_expectation_collector_requires_representative_tenant_knowledge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _operator(tmp_path, RecordingBackupRunner())
    plan = operator.plan("tenant-a", backup_id="backup-expectation-empty")

    class EmptyGovernance:
        def __init__(self, _database_url: str) -> None:
            pass

        async def list_candidates(self, _tenant_id: str) -> tuple[KnowledgeCandidate, ...]:
            return ()

    monkeypatch.setattr(
        "agent_memory_service.host_backup.PostgresGovernanceStore",
        EmptyGovernance,
    )
    collector = PostgresBackupExpectationCollector(
        tmp_path / "tenant-secrets",
        postgres_host="postgres.internal",
        postgres_port=5432,
    )

    with pytest.raises(BackupValidationError, match="representative published"):
        collector.collect(plan)


def test_create_uses_fixed_argv_restarts_neo4j_and_publishes_manifest_last(
    tmp_path: Path,
) -> None:
    runner = RecordingBackupRunner()
    replacements: list[Path] = []
    operator = _operator(tmp_path, runner, replacements=replacements)

    manifest = operator.create("tenant-a", backup_id="backup-tenant-a-001")

    assert manifest.complete
    published = tmp_path / "artifacts" / manifest.backup_id
    assert (published / "manifest.json").is_file()
    receipt = tmp_path / "evidence" / f"{manifest.backup_id}.json"
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o077 == 0
    assert json.loads(receipt.read_text(encoding="utf-8"))["tenant_id"] == "tenant-a"
    assert replacements[-1] == published / "manifest.json"
    assert all(
        (published / f"{artifact.store.value}.age").is_file() for artifact in manifest.artifacts
    )
    assert not list((tmp_path / "staging").rglob("*.dump"))
    command_text = "\n".join(" ".join(command) for command in runner.commands)
    assert "control-secret" not in command_text
    assert "tenant-secret" not in command_text
    assert "/bin/sh" not in command_text
    assert "database dump neo4j" in command_text
    assert "--network none" in command_text
    assert "--cap-drop ALL" in command_text
    stop_index = next(
        index for index, command in enumerate(runner.commands) if command[-2:] == ("stop", "neo4j")
    )
    archive_index = next(
        index for index, command in enumerate(runner.commands) if command[:2] == ("docker", "run")
    )
    archive_command = runner.commands[archive_index]
    assert archive_command[archive_command.index("--user") + 1] == f"7474:{os.getgid()}"
    assert "memory-tenant-tenant-a-neo4j-data:/data" in archive_command
    assert "memory-tenant-tenant-a-neo4j-data:/data:ro" not in archive_command
    assert (
        f"/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777,uid=7474,gid={os.getgid()}"
        in archive_command
    )
    assert (
        f"/logs:rw,nosuid,nodev,noexec,size=64m,mode=0750,uid=7474,gid={os.getgid()}"
        in archive_command
    )
    assert runner.neo4j_handoff_modes == [0o770]
    assert archive_command[archive_command.index("--entrypoint") + 1] == (
        "/var/lib/neo4j/bin/neo4j-admin"
    )
    assert "neo4j-admin" not in archive_command[archive_command.index("--entrypoint") + 2 :]
    restart_index = next(
        index
        for index, command in enumerate(runner.commands)
        if command[-4:] == ("up", "-d", "--wait", "neo4j")
    )
    assert stop_index < archive_index < restart_index
    postgres_environments = [
        environment
        for command, environment in zip(runner.commands, runner.environments, strict=True)
        if command[0] == "pg_dump"
    ]
    assert [environment and environment["PGPASSWORD"] for environment in postgres_environments] == [
        "control-secret",
        "tenant-secret",
    ]


def test_failed_neo4j_archive_restarts_only_that_tenant_and_publishes_no_manifest(
    tmp_path: Path,
) -> None:
    runner = RecordingBackupRunner(fail_neo4j_archive=True)
    operator = _operator(tmp_path, runner)

    with pytest.raises(RuntimeError, match="archive failed"):
        operator.create("tenant-a", backup_id="backup-tenant-a-failed")

    assert any(command[-4:] == ("up", "-d", "--wait", "neo4j") for command in runner.commands)
    assert not (tmp_path / "artifacts" / "backup-tenant-a-failed" / "manifest.json").exists()
    assert not list((tmp_path / "staging").rglob(".neo4j-dump"))


def test_verify_detects_corruption_and_restore_drill_fails_closed_when_unconfigured(
    tmp_path: Path,
) -> None:
    operator = _operator(tmp_path, RecordingBackupRunner())
    manifest = operator.create("tenant-a", backup_id="backup-tenant-a-verify")
    manifest_path = tmp_path / "artifacts" / manifest.backup_id / "manifest.json"

    assert operator.verify(manifest_path) == manifest
    with pytest.raises(RestoreSafetyError, match="not configured"):
        operator.restore_drill(
            manifest_path,
            target_id="restore-drill-tenant-a-001",
            operator_id="operator-alice",
        )
    with pytest.raises(RestoreSafetyError, match="not configured"):
        operator.restore_latest_drill(
            "tenant-a",
            target_id="restore-drill-tenant-a-latest",
            operator_id="operator-alice",
        )
    (manifest_path.parent / "tenant-postgres.age").write_bytes(b"corrupt")
    with pytest.raises(BackupValidationError, match="mismatch"):
        operator.verify(manifest_path)


def test_memoryctl_backup_plan_create_and_verify_use_the_host_operator(tmp_path: Path) -> None:
    control = _control()
    operator = _operator(tmp_path, RecordingBackupRunner())
    output = io.StringIO()

    run_cli(
        ["backup", "plan", "--tenant-id", "tenant-a", "--backup-id", "backup-cli-001"],
        control,
        output,
        backup=operator,
    )
    run_cli(
        ["backup", "create", "--tenant-id", "tenant-a", "--backup-id", "backup-cli-001"],
        control,
        output,
        backup=operator,
    )
    manifest_path = tmp_path / "artifacts" / "backup-cli-001" / "manifest.json"
    run_cli(
        ["backup", "verify", "--manifest", str(manifest_path)],
        control,
        output,
        backup=operator,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["operation"] == "plan"
    assert documents[1]["operation"] == "create"
    assert documents[1]["manifest"]["complete"] is True
    assert documents[2] == {
        "backup_id": "backup-cli-001",
        "operation": "verify",
        "valid": True,
    }


def test_memoryctl_inspects_and_exactly_confirms_abandoned_barrier_recovery(
    tmp_path: Path,
) -> None:
    barrier = RecordingConsistencyBarrier()
    barrier.record = BackupBarrierRecord(
        tenant_id="tenant-a",
        barrier_id="barrier-123",
        started_at=NOW,
    )
    operator = _operator(
        tmp_path,
        RecordingBackupRunner(),
        consistency_barrier=barrier,
    )
    output = io.StringIO()

    run_cli(
        ["backup", "barrier-status", "--tenant-id", "tenant-a"],
        _control(),
        output,
        backup=operator,
    )
    with pytest.raises(ValueError, match="TENANT_ID:BARRIER_ID"):
        run_cli(
            [
                "backup",
                "recover-barrier",
                "--tenant-id",
                "tenant-a",
                "--barrier-id",
                "barrier-123",
                "--confirm",
                "tenant-a",
            ],
            _control(),
            backup=operator,
        )
    run_cli(
        [
            "backup",
            "recover-barrier",
            "--tenant-id",
            "tenant-a",
            "--barrier-id",
            "barrier-123",
            "--confirm",
            "tenant-a:barrier-123",
        ],
        _control(),
        output,
        backup=operator,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["barrier"]["barrier_id"] == "barrier-123"
    assert documents[1]["recovered"] is True
    assert ("recover", "tenant-a:barrier-123") in barrier.calls


def test_memoryctl_restore_drill_requires_exact_target_confirmation(tmp_path: Path) -> None:
    control = _control()
    operator = _operator(tmp_path, RecordingBackupRunner())
    manifest = operator.create("tenant-a", backup_id="backup-cli-restore")
    manifest_path = tmp_path / "artifacts" / manifest.backup_id / "manifest.json"
    arguments = [
        "backup",
        "restore-drill",
        "--manifest",
        str(manifest_path),
        "--target-id",
        "restore-drill-tenant-a-001",
        "--operator-id",
        "operator-alice",
        "--confirm-target",
    ]

    with pytest.raises(ValueError, match="confirmation"):
        run_cli([*arguments, "wrong-target"], control, backup=operator)
    with pytest.raises(RestoreSafetyError, match="not configured"):
        run_cli(
            [*arguments, "restore-drill-tenant-a-001"],
            control,
            backup=operator,
        )
    latest_arguments = [
        "backup",
        "restore-latest-drill",
        "--tenant-id",
        "tenant-a",
        "--target-id",
        "restore-drill-tenant-a-latest",
        "--operator-id",
        "operator-alice",
        "--confirm-target",
    ]
    with pytest.raises(ValueError, match="confirmation"):
        run_cli([*latest_arguments, "wrong-target"], control, backup=operator)
    with pytest.raises(RestoreSafetyError, match="not configured"):
        run_cli(
            [*latest_arguments, "restore-drill-tenant-a-latest"],
            control,
            backup=operator,
        )


def test_host_retention_and_rpo_operate_only_on_verified_exact_backup_sets(
    tmp_path: Path,
) -> None:
    operator = _operator(tmp_path, RecordingBackupRunner())
    operator.create("tenant-a", backup_id="backup-retention-a")
    operator.create("tenant-a", backup_id="backup-retention-b")

    plan = operator.retention_plan("tenant-a")
    execution = operator.apply_retention("tenant-a")
    health = operator.health("tenant-a")

    assert len(plan.keep_backup_ids) == 1
    assert len(plan.delete_backup_ids) == 1
    assert execution.deleted_backup_ids == plan.delete_backup_ids
    assert execution.failed_backup_ids == ()
    assert not (tmp_path / "artifacts" / plan.delete_backup_ids[0]).exists()
    assert not (tmp_path / "evidence" / f"{plan.delete_backup_ids[0]}.json").exists()
    assert health.latest_valid_backup_id in plan.keep_backup_ids
    assert not health.overdue


def test_host_retention_exposes_and_keeps_decommission_pin(tmp_path: Path) -> None:
    pin = RetentionPin(
        backup_id="backup-retention-pinned",
        reason="open-decommission:grace-period",
        protected_until=NOW + timedelta(days=30),
    )
    protection = RetentionProtection([(pin,)])
    operator = _operator(
        tmp_path,
        RecordingBackupRunner(),
        retention_protection=protection,
    )
    operator.create("tenant-a", backup_id="backup-retention-keep")
    operator.create("tenant-a", backup_id=pin.backup_id)

    plan = operator.retention_plan("tenant-a")
    output = io.StringIO()
    run_cli(
        ["backup", "retention-plan", "--tenant-id", "tenant-a"],
        _control(),
        output,
        backup=operator,
    )
    document = json.loads(output.getvalue())

    assert plan.delete_backup_ids == ()
    assert plan.pins == (pin,)
    assert document["plan"]["pins"][0]["status"] == "pinned"
    assert document["plan"]["pins"][0]["reason"] == pin.reason
    assert protection.calls == [("tenant-a", NOW), ("tenant-a", NOW)]


def test_host_retention_rechecks_pin_immediately_before_delete(tmp_path: Path) -> None:
    pin = RetentionPin(
        backup_id="backup-retention-race-b",
        reason="open-decommission:suspended",
    )
    protection = RetentionProtection([(), (pin,)])
    operator = _operator(
        tmp_path,
        RecordingBackupRunner(),
        retention_protection=protection,
    )
    operator.create("tenant-a", backup_id="backup-retention-race-a")
    operator.create("tenant-a", backup_id=pin.backup_id)

    execution = operator.apply_retention("tenant-a")

    assert execution.deleted_backup_ids == ()
    assert execution.failed_backup_ids == (pin.backup_id,)
    assert (tmp_path / "artifacts" / pin.backup_id).is_dir()


def test_memoryctl_reports_backup_health_and_requires_exact_retention_confirmation(
    tmp_path: Path,
) -> None:
    control = _control()
    operator = _operator(tmp_path, RecordingBackupRunner())
    operator.create("tenant-a", backup_id="backup-retention-cli-a")
    operator.create("tenant-a", backup_id="backup-retention-cli-b")
    output = io.StringIO()

    run_cli(
        ["backup", "status", "--tenant-id", "tenant-a"],
        control,
        output,
        backup=operator,
    )
    run_cli(
        ["backup", "retention-plan", "--tenant-id", "tenant-a"],
        control,
        output,
        backup=operator,
    )
    with pytest.raises(ValueError, match="Retention confirmation"):
        run_cli(
            [
                "backup",
                "retention-apply",
                "--tenant-id",
                "tenant-a",
                "--confirm",
                "tenant-b",
            ],
            control,
            backup=operator,
        )
    run_cli(
        [
            "backup",
            "retention-apply",
            "--tenant-id",
            "tenant-a",
            "--confirm",
            "tenant-a",
        ],
        control,
        output,
        backup=operator,
    )

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    assert documents[0]["operation"] == "status"
    assert documents[0]["health"]["overdue"] is False
    assert documents[1]["operation"] == "retention-plan"
    assert len(documents[1]["plan"]["delete_backup_ids"]) == 1
    assert documents[2]["operation"] == "retention-apply"
    assert documents[2]["result"]["failed_backup_ids"] == []


def test_local_retention_refuses_a_backup_directory_with_unexpected_files(
    tmp_path: Path,
) -> None:
    operator = _operator(tmp_path, RecordingBackupRunner())
    operator.create("tenant-a", backup_id="backup-retention-safe")
    operator.create("tenant-a", backup_id="backup-retention-unsafe")
    plan = operator.retention_plan("tenant-a")
    deletion_target = tmp_path / "artifacts" / plan.delete_backup_ids[0]
    (deletion_target / "operator-note.txt").write_text("preserve", encoding="utf-8")

    execution = operator.apply_retention("tenant-a")

    assert execution.deleted_backup_ids == ()
    assert execution.failed_backup_ids == plan.delete_backup_ids
    assert deletion_target.is_dir()
