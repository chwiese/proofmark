"""Orchestration: run the test suite, then apply the quality gates."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from proofmark import ratchet
from proofmark.config import BASELINE_NAME, Config
from proofmark.ratchet import Baseline, Report

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"


def _colour(code: str, text: str) -> str:
    """Wrap text in an ANSI colour, unless output is redirected."""
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def status(message: str) -> None:
    """Print a progress line.

    Flushed because the subprocesses below write straight to the inherited
    stdout; without this our buffered output would appear after theirs.
    """
    print(f"{_colour(GREEN, '[✓]')} {message}", flush=True)


def warn(message: str) -> None:
    """Print a warning line."""
    print(f"{_colour(YELLOW, '[!]')} {message}", flush=True)


def fail(message: str) -> None:
    """Print a failure line."""
    print(f"{_colour(RED, '[✗]')} {message}", flush=True)


def _env_for_uv() -> dict[str, str]:
    """Build a subprocess environment safe for `uv run`.

    When proofmark runs as a pre-commit/prek hook it executes inside the hook's
    own isolated virtualenv, which sets VIRTUAL_ENV. `uv run` resolves the
    project environment from the working directory regardless, but it emits a
    warning about the mismatch on every invocation. Dropping the variable keeps
    the output clean without changing which environment is used.
    """
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def run_uv(args: Sequence[str], config: Config) -> int:
    """Run a command in the checked project's environment via uv.

    Args:
        args: Arguments following `uv run`.
        config: The resolved project configuration.

    Returns:
        The command's exit status.
    """
    sys.stdout.flush()
    completed = subprocess.run(
        ["uv", "run", *args],
        cwd=config.root,
        env=_env_for_uv(),
        check=False,
    )
    return completed.returncode


def is_installed(module: str, config: Config) -> bool:
    """Check whether a module is importable in the checked project.

    proofmark declares no dependencies of its own and resolves pytest,
    diff-cover and mutmut from the project instead. Probing first turns "you
    have not installed this" into a clear message rather than an opaque
    failure from uv, or - for mutmut, whose exit status is deliberately
    ignored - a silent no-op.

    Args:
        module: Importable module name.
        config: The resolved project configuration.

    Returns:
        True if the module can be imported in the project environment.
    """
    sys.stdout.flush()
    completed = subprocess.run(
        ["uv", "run", "python", "-c", f"import {module}"],
        cwd=config.root,
        env=_env_for_uv(),
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def run_pytest(config: Config) -> int:
    """Run the test suite with coverage reporting.

    Coverage flags are passed here rather than living in the project's pytest
    addopts so that ad-hoc runs (`uv run pytest -k foo`) stay fast, and so
    mutmut - which re-runs pytest once per mutant - does not pay for coverage
    instrumentation every time.

    Args:
        config: The resolved project configuration.

    Returns:
        pytest's exit status.
    """
    return run_uv(
        [
            "pytest",
            f"--cov={config.source}",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=json",
            "--cov-report=xml",
        ],
        config,
    )


def _print_regressions(regressions: Sequence[ratchet.Regression]) -> None:
    """Report each file that fell below its floor, worst first.

    Laid out as an aligned table so the size of each drop is comparable at a
    glance - which of several regressions to fix first is usually the question.
    """
    width = max(len(item.path) for item in regressions)
    width = max(width, len("FILE"))

    print(f"Coverage regressed against {BASELINE_NAME}\n")
    print(f"  {'FILE':<{width}}  {'BASELINE':>9}  {'NOW':>9}  {'DROP':>8}")
    print(f"  {'─' * (width + 32)}")
    for item in regressions:
        print(
            f"  {item.path:<{width}}  "
            f"{item.baseline:>8.2f}%  "
            f"{item.current:>8.2f}%  "
            f"{-item.drop:>8.2f}"
        )
    print(
        "\n  Add tests for the lines you uncovered, or state the reduction\n"
        f"  deliberately by editing {BASELINE_NAME}."
    )


def check_ratchet(config: Config, *, update: bool) -> int:
    """Apply the per-file coverage gate.

    Args:
        config: The resolved project configuration.
        update: Whether to raise the baseline on success.

    Returns:
        0 if coverage held or improved, 1 otherwise.
    """
    report: Report = ratchet.read_report(config.coverage_json)
    baseline: Baseline = ratchet.read_baseline(config.baseline)

    regressions = ratchet.find_regressions(report, baseline)
    if regressions:
        _print_regressions(regressions)
        return 1

    if ratchet.total_regressed(report, baseline):
        print(
            f"Total coverage regressed: baseline {baseline['total']:.2f}% "
            f"-> now {report.total:.2f}%"
        )
        return 1

    if update:
        updated = ratchet.raise_baseline(report, baseline)
        ratchet.write_baseline(config.baseline, updated)
        gained = updated["total"] - baseline["total"]
        if gained > ratchet.EPSILON:
            print(
                f"Coverage baseline raised: {baseline['total']:.2f}% "
                f"-> {updated['total']:.2f}% (+{gained:.2f})"
            )
        else:
            print(f"Coverage baseline held at {updated['total']:.2f}%")
    else:
        print(
            f"Coverage OK: {report.total:.2f}% total, "
            f"no file below its baseline ({len(baseline['files'])} tracked)"
        )
    return 0


def _compare_branch_exists(config: Config) -> bool:
    """Check whether the configured comparison branch is resolvable."""
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", config.compare_branch],
        cwd=config.root,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def check_diff_coverage(config: Config) -> int:
    """Apply the diff coverage gate to lines changed against the base branch.

    Args:
        config: The resolved project configuration.

    Returns:
        0 if the gate passed or was skipped, non-zero otherwise.
    """
    if not _compare_branch_exists(config):
        warn(f"{config.compare_branch} not found - skipping diff coverage")
        return 0

    if not is_installed("diff_cover", config):
        fail(
            "diff-cover is not installed in this project.\n"
            "    Add it with: uv add --dev diff-cover"
        )
        return 1

    status(f"Checking diff coverage against {config.compare_branch}...")
    return run_uv(
        [
            "diff-cover",
            str(config.coverage_xml.name),
            f"--compare-branch={config.compare_branch}",
            f"--fail-under={config.diff_threshold}",
        ],
        config,
    )


def run_mutation_testing(config: Config) -> None:
    """Run mutmut and point the user at the survivor list.

    Mutation testing is advisory. mutmut exits non-zero whenever mutants
    survive, which is information rather than a failure: a surviving mutant is
    evidence the tests do not distinguish a behaviour, not proof of a bug.
    Gating on the score encourages writing assertions that kill mutants instead
    of assertions that describe behaviour, so the exit status is ignored here.

    Because that status is ignored, a missing mutmut would otherwise look
    exactly like a successful run - hence the explicit check first.

    Args:
        config: The resolved project configuration.
    """
    if not is_installed("mutmut", config):
        warn(
            "mutmut is not installed in this project - skipping mutation testing.\n"
            "    Add it with: uv add --dev mutmut"
        )
        return

    status("Running mutation testing (advisory - does not gate)...")
    run_uv(["mutmut", "run"], config)
    status("Review survivors with: uv run mutmut browse")
