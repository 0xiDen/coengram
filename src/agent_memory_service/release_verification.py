"""Provider-neutral release verification with explicit phase evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic

CommandRunner = Callable[[tuple[str, ...]], int]


class PhaseStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Phase:
    """One independently reported release suite."""

    name: str
    command: tuple[str, ...] | None = None
    required: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class PhaseResult:
    name: str
    status: PhaseStatus
    duration_seconds: float
    required: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "duration_seconds": round(self.duration_seconds, 3),
            "required": self.required,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReleaseReport:
    results: tuple[PhaseResult, ...]

    @property
    def failures(self) -> int:
        return sum(result.status is PhaseStatus.FAIL for result in self.results)

    @property
    def required_skips(self) -> int:
        return sum(result.status is PhaseStatus.SKIP and result.required for result in self.results)

    @property
    def succeeded(self) -> bool:
        return self.failures == 0 and self.required_skips == 0

    def to_json(self) -> str:
        passed = sum(result.status is PhaseStatus.PASS for result in self.results)
        skipped = sum(result.status is PhaseStatus.SKIP for result in self.results)
        return json.dumps(
            {
                "succeeded": self.succeeded,
                "summary": {
                    "passed": passed,
                    "failed": self.failures,
                    "skipped": skipped,
                    "required_skips": self.required_skips,
                },
                "phases": [result.as_dict() for result in self.results],
            },
            indent=2,
            sort_keys=True,
        )


def run_command(command: tuple[str, ...]) -> int:
    return subprocess.run(command, check=False).returncode


def write_line(line: str) -> None:
    print(line, flush=True)


class ReleaseVerifier:
    """Run release phases while retaining fail-fast behavior and complete evidence."""

    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        clock: Callable[[], float] = monotonic,
        write_line: Callable[[str], None] = write_line,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._write_line = write_line

    def run(self, phases: Sequence[Phase]) -> ReleaseReport:
        results: list[PhaseResult] = []
        blocked_by: str | None = None
        self._write_line("Release verification")

        for phase in phases:
            if phase.unavailable_reason is not None:
                result = PhaseResult(
                    name=phase.name,
                    status=PhaseStatus.SKIP,
                    duration_seconds=0.0,
                    required=phase.required,
                    reason=phase.unavailable_reason,
                )
            elif blocked_by is not None:
                result = PhaseResult(
                    name=phase.name,
                    status=PhaseStatus.SKIP,
                    duration_seconds=0.0,
                    required=phase.required,
                    reason=f"blocked by {blocked_by} failure",
                )
            elif phase.command is None:
                result = PhaseResult(
                    name=phase.name,
                    status=PhaseStatus.SKIP,
                    duration_seconds=0.0,
                    required=phase.required,
                    reason="no verification command configured",
                )
            else:
                self._write_line(f"RUN  {phase.name}")
                started = self._clock()
                exit_code = self._runner(phase.command)
                duration = self._clock() - started
                if exit_code == 0:
                    result = PhaseResult(
                        name=phase.name,
                        status=PhaseStatus.PASS,
                        duration_seconds=duration,
                        required=phase.required,
                    )
                else:
                    result = PhaseResult(
                        name=phase.name,
                        status=PhaseStatus.FAIL,
                        duration_seconds=duration,
                        required=phase.required,
                        reason=f"exit {exit_code}",
                    )
                    blocked_by = phase.name
            results.append(result)
            self._write_result(result)

        report = ReleaseReport(tuple(results))
        passed = sum(result.status is PhaseStatus.PASS for result in report.results)
        skipped = sum(result.status is PhaseStatus.SKIP for result in report.results)
        self._write_line(
            "SUMMARY "
            f"pass={passed} fail={report.failures} skip={skipped} "
            f"required_skips={report.required_skips}"
        )
        self._write_line("RELEASE PASS" if report.succeeded else "RELEASE FAIL")
        return report

    def _write_result(self, result: PhaseResult) -> None:
        required = " required" if result.required and result.status is PhaseStatus.SKIP else ""
        reason = f" ({result.reason})" if result.reason else ""
        self._write_line(
            f"{result.status.value.upper():4} {result.name} "
            f"{result.duration_seconds:.2f}s{required}{reason}"
        )


def _command_succeeds(command: tuple[str, ...]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def default_phases() -> tuple[Phase, ...]:
    python = sys.executable
    docker_cli = shutil.which("docker") is not None
    compose_available = docker_cli and _command_succeeds(("docker", "compose", "version"))
    daemon_available = docker_cli and _command_succeeds(("docker", "info"))
    compose_reason = None if compose_available else "Docker Compose v2 is unavailable"
    daemon_reason = None if daemon_available else "Docker daemon is unavailable"

    return (
        Phase("public-tree", (python, "scripts/verify-public-tree.py")),
        Phase("format-check", (python, "-m", "ruff", "format", "--check", ".")),
        Phase("lint", (python, "-m", "ruff", "check", ".")),
        Phase("typecheck", (python, "-m", "mypy", "--no-incremental", "src", "tests")),
        Phase(
            "unit-and-contract-tests",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "--ignore=tests/integration",
                "--ignore=tests/test_release_tracer.py",
                "--ignore=tests/test_deploy_configs.py",
                "--ignore=tests/test_telemetry.py",
            ),
        ),
        Phase(
            "release-tracer",
            (python, "-m", "pytest", "-q", "tests/test_release_tracer.py"),
        ),
        Phase(
            "deployment-and-observability-contracts",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "tests/test_deploy_configs.py",
                "tests/test_telemetry.py",
            ),
        ),
        Phase("compose-config", ("make", "config"), unavailable_reason=compose_reason),
        Phase(
            "caddy-image-contract",
            ("sh", "scripts/verify-caddy.sh"),
            unavailable_reason=daemon_reason,
        ),
        Phase(
            "container-integration",
            ("sh", "scripts/verify-infrastructure.sh"),
            unavailable_reason=daemon_reason,
        ),
        Phase(
            "operator-cloudflare-ingress",
            required=False,
            unavailable_reason=(
                "operator-only: requires deployed Cloudflare-proxied DNS, certificate issuance, "
                "and the public hostname"
            ),
        ),
        Phase(
            "operator-anthropic-smoke",
            required=False,
            unavailable_reason=(
                "operator-only: requires an explicitly supplied live Anthropic credential"
            ),
        ),
        Phase(
            "operator-telegram-delivery",
            required=False,
            unavailable_reason=(
                "operator-only: requires an explicitly configured bot and public webhook"
            ),
        ),
        Phase(
            "operator-observability-pipeline",
            required=False,
            unavailable_reason=(
                "operator-only: requires the deployed Alloy, Mimir, Loki, Tempo, and "
                "operator-accessible Grafana stack"
            ),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-report",
        type=Path,
        help="also write machine-readable release evidence to this path",
    )
    args = parser.parse_args(argv)

    repository_root = Path(__file__).resolve().parents[2]
    os.chdir(repository_root)
    report = ReleaseVerifier().run(default_phases())
    if args.json_report is not None:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"JSON report: {args.json_report}")
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
