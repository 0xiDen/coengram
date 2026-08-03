from __future__ import annotations

import json
from collections.abc import Iterable

from agent_memory_service.release_verification import (
    Phase,
    PhaseStatus,
    ReleaseVerifier,
    default_phases,
)


class StubRunner:
    def __init__(self, exit_codes: Iterable[int]) -> None:
        self._exit_codes = iter(exit_codes)
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> int:
        self.commands.append(command)
        return next(self._exit_codes)


class StepClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_public_tree_guard_is_the_first_default_release_phase() -> None:
    first = default_phases()[0]

    assert first.name == "public-tree"
    assert first.command is not None
    assert first.command[-1] == "scripts/verify-public-tree.py"


def test_release_report_names_phases_durations_skips_and_failures() -> None:
    output: list[str] = []
    runner = StubRunner([0, 7])
    verifier = ReleaseVerifier(
        runner=runner,
        clock=StepClock([10.0, 10.25, 20.0, 21.5]),
        write_line=output.append,
    )

    report = verifier.run(
        (
            Phase("format-check", ("python", "-m", "ruff")),
            Phase("unit-contract", ("python", "-m", "pytest")),
            Phase("release-tracer", ("python", "-m", "pytest", "tracer")),
            Phase(
                "operator-cloudflare-tls",
                required=False,
                unavailable_reason="requires the deployed public hostname",
            ),
        )
    )

    assert [result.status for result in report.results] == [
        PhaseStatus.PASS,
        PhaseStatus.FAIL,
        PhaseStatus.SKIP,
        PhaseStatus.SKIP,
    ]
    assert report.results[0].duration_seconds == 0.25
    assert report.results[1].duration_seconds == 1.5
    assert report.results[2].reason == "blocked by unit-contract failure"
    assert report.results[3].reason == "requires the deployed public hostname"
    assert report.succeeded is False
    assert runner.commands == [
        ("python", "-m", "ruff"),
        ("python", "-m", "pytest"),
    ]
    assert any("PASS format-check" in line and "0.25s" in line for line in output)
    assert any("FAIL unit-contract" in line and "exit 7" in line for line in output)
    assert any("SKIP release-tracer" in line and "blocked" in line for line in output)


def test_required_capability_skip_fails_gate_without_hiding_later_independent_phase() -> None:
    runner = StubRunner([0])
    report = ReleaseVerifier(runner=runner, write_line=lambda _line: None).run(
        (
            Phase(
                "compose-config",
                ("make", "config"),
                unavailable_reason="Docker Compose is unavailable",
            ),
            Phase("provider-neutral-tracer", ("python", "-m", "pytest", "tracer")),
        )
    )

    assert report.results[0].status is PhaseStatus.SKIP
    assert report.results[0].required is True
    assert report.results[1].status is PhaseStatus.PASS
    assert report.succeeded is False
    assert report.required_skips == 1


def test_release_report_has_machine_readable_evidence() -> None:
    report = ReleaseVerifier(
        runner=StubRunner([0]),
        clock=StepClock([2.0, 2.125]),
        write_line=lambda _line: None,
    ).run((Phase("lint", ("python", "-m", "ruff", "check")),))

    document = json.loads(report.to_json())

    assert document == {
        "succeeded": True,
        "summary": {"failed": 0, "passed": 1, "required_skips": 0, "skipped": 0},
        "phases": [
            {
                "duration_seconds": 0.125,
                "name": "lint",
                "reason": None,
                "required": True,
                "status": "pass",
            }
        ],
    }
