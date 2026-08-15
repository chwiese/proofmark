"""Tests for the command line interface.

These cover the order the gates run in and how failures short-circuit, since
that is what determines whether a bad push is actually blocked.
"""

from pathlib import Path

import pytest

from proofmark import cli, runner
from proofmark.config import Config, ConfigError
from proofmark.ratchet import RatchetError


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        root=tmp_path, source="mypkg", diff_threshold=80, compare_branch="origin/main"
    )


@pytest.fixture
def stub_gates(monkeypatch: pytest.MonkeyPatch, config: Config) -> list[str]:
    """Record which stages ran, with every stage succeeding by default.

    Individual tests override a single stage to return a failure and then
    assert on which later stages were skipped.
    """
    order: list[str] = []

    def stub_pytest(_config: Config) -> int:
        order.append("pytest")
        return 0

    def stub_ratchet(_config: Config, update: bool) -> int:
        order.append(f"ratchet(update={update})")
        return 0

    def stub_diff(_config: Config) -> int:
        order.append("diff")
        return 0

    def stub_mutants(_config: Config) -> None:
        order.append("mutants")

    monkeypatch.setattr(cli, "load", lambda: config)
    monkeypatch.setattr(runner, "run_pytest", stub_pytest)
    monkeypatch.setattr(runner, "check_ratchet", stub_ratchet)
    monkeypatch.setattr(runner, "check_diff_coverage", stub_diff)
    monkeypatch.setattr(runner, "run_mutation_testing", stub_mutants)
    return order


def test_run_executes_every_gate_in_order(stub_gates: list[str]) -> None:
    assert cli.main(["run"]) == 0
    assert stub_gates == ["pytest", "ratchet(update=True)", "diff"]


def test_check_mode_does_not_update_the_baseline(stub_gates: list[str]) -> None:
    assert cli.main(["run", "--check"]) == 0
    assert "ratchet(update=False)" in stub_gates


def test_mutants_flag_adds_mutation_testing(stub_gates: list[str]) -> None:
    assert cli.main(["run", "--mutants"]) == 0
    assert stub_gates[-1] == "mutants"


def test_mutation_testing_is_off_by_default(stub_gates: list[str]) -> None:
    cli.main(["run"])
    assert "mutants" not in stub_gates


def test_failing_tests_skip_the_gates(
    monkeypatch: pytest.MonkeyPatch, stub_gates: list[str]
) -> None:
    """No point ratcheting coverage from a run that did not complete."""
    monkeypatch.setattr(runner, "run_pytest", lambda c: 1)

    assert cli.main(["run"]) == 1
    assert stub_gates == []


def test_an_empty_suite_still_seeds_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
    stub_gates: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project with no tests yet is adopting the ratchet, not failing it.

    pytest exits 5 when it collects nothing. Treating that as a failure would
    make the gate impossible to install before the first test exists.
    """
    monkeypatch.setattr(runner, "run_pytest", lambda c: cli.PYTEST_NO_TESTS_COLLECTED)

    assert cli.main(["run"]) == 0
    assert "ratchet(update=True)" in stub_gates
    assert "No tests collected" in capsys.readouterr().out


def test_ratchet_failure_skips_diff_coverage(
    monkeypatch: pytest.MonkeyPatch, stub_gates: list[str]
) -> None:
    monkeypatch.setattr(runner, "check_ratchet", lambda c, update: 1)

    assert cli.main(["run"]) == 1
    assert "diff" not in stub_gates


def test_diff_coverage_failure_is_propagated(
    monkeypatch: pytest.MonkeyPatch, stub_gates: list[str]
) -> None:
    monkeypatch.setattr(runner, "check_diff_coverage", lambda c: 1)

    assert cli.main(["run"]) == 1


def test_mutation_testing_is_skipped_when_a_gate_fails(
    monkeypatch: pytest.MonkeyPatch, stub_gates: list[str]
) -> None:
    monkeypatch.setattr(runner, "check_diff_coverage", lambda c: 1)

    cli.main(["run", "--mutants"])

    assert "mutants" not in stub_gates


# The standalone check subcommand


def test_check_subcommand_runs_the_gate_only(stub_gates: list[str]) -> None:
    assert cli.main(["check"]) == 0
    assert stub_gates == ["ratchet(update=False)"]


def test_check_update_raises_the_baseline(stub_gates: list[str]) -> None:
    assert cli.main(["check", "--update"]) == 0
    assert stub_gates == ["ratchet(update=True)"]


# Error handling


@pytest.mark.parametrize(
    "error", [ConfigError("bad config"), RatchetError("no report")]
)
def test_expected_errors_exit_with_status_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """Status 2 distinguishes a setup problem from a failed quality gate."""

    def raise_error() -> Config:
        raise error

    monkeypatch.setattr(cli, "load", raise_error)

    assert cli.main(["run"]) == 2
    assert str(error) in capsys.readouterr().out


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
