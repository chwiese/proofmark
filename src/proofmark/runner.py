"""Orchestration: run the test suite, then apply the quality gates."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from proofmark import mutants, ratchet
from proofmark.config import BASELINE_NAME, Config
from proofmark.ratchet import Baseline, Report

# Where mutmut caches which tests reach which functions, under `mutants/`.
MUTMUT_STATS_NAME = "mutmut-stats.json"

# pytest's exit status when it collected nothing at all. For a ratchet, an
# empty suite is a legitimate starting state rather than a failure: seeding a
# baseline at zero is exactly how a project adopts the gate before it has
# written its first test.
PYTEST_NO_TESTS_COLLECTED = 5

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


def run_uv_captured(args: Sequence[str], config: Config) -> tuple[int, str]:
    """Run a command in the checked project and read back what it printed.

    Args:
        args: Arguments following `uv run`.
        config: The resolved project configuration.

    Returns:
        The command's exit status and its standard output.
    """
    sys.stdout.flush()
    completed = subprocess.run(
        ["uv", "run", *args],
        cwd=config.root,
        env=_env_for_uv(),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


def is_installed(module: str, config: Config) -> bool:
    """Check whether a module is importable in the checked project.

    proofmark declares no dependencies of its own and resolves pytest,
    diff-cover and mutmut from the project instead. Probing first turns "you
    have not installed this" into a clear message rather than an opaque
    failure from uv, or - for the whole-tree mutmut sweep, which nothing fails
    on - a silent no-op.

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


def _how_to_record(verb: str) -> str:
    """Spell out the remedy for a baseline that has fallen behind.

    `check --update` leads because the report on disk is the one these numbers
    were just read from - it records exactly what was reported, and costs
    nothing. Every route to this message has already written that report.
    `run` re-measures, which is what you want only if the tree has moved since.

    Args:
        verb: How to open the sentence, "Record" or "Create".

    Returns:
        An indented block ready to print.
    """
    return (
        f"  {verb} it with: proofmark check --update, which records the\n"
        f"  report already measured - or proofmark run to re-measure first.\n"
        f"  Then commit {BASELINE_NAME}."
    )


def _print_unrecorded(
    unrecorded: Sequence[ratchet.Unrecorded],
    *,
    recorded_total: float,
    current_total: float,
) -> None:
    """Report coverage the project has earned but never committed.

    Laid out like the regression table, since the question is the same one in
    reverse: how far the committed standard has fallen behind what the suite
    actually achieves.
    """
    width = max(len(item.path) for item in unrecorded)
    width = max(width, len("FILE"))

    print(f"{BASELINE_NAME} is out of date - these gains are not recorded\n")
    print(f"  {'FILE':<{width}}  {'BASELINE':>9}  {'NOW':>9}  {'GAIN':>8}")
    print(f"  {'─' * (width + 32)}")
    for item in unrecorded:
        # An untracked file has no floor at all, rather than a floor of zero.
        floor = "(new)" if item.baseline is None else f"{item.baseline:.2f}%"
        print(
            f"  {item.path:<{width}}  "
            f"{floor:>9}  "
            f"{item.current:>8.2f}%  "
            f"{item.gain:>+8.2f}"
        )
    print(f"\n  Total: {recorded_total:.2f}% -> {current_total:.2f}%")
    print(
        "\n  An unrecorded gain is protected by nothing: give it back later\n"
        "  and no gate will notice.\n"
    )
    print(_how_to_record("Record"))


def _check_staleness(config: Config, report: Report, baseline: Baseline) -> int:
    """Fail when the committed baseline understates the project.

    Only reached in check mode. The updating path answers the same question by
    writing the gain down, so it has nothing to complain about.

    Args:
        config: The resolved project configuration.
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        0 if the baseline is current, 1 otherwise.
    """
    if not config.baseline.exists():
        print("No baseline recorded yet - nothing is gating this project.\n")
        print(_how_to_record("Create"))
        return 1

    unrecorded = ratchet.find_unrecorded(report, baseline)
    if unrecorded:
        _print_unrecorded(
            unrecorded,
            recorded_total=baseline["total"],
            current_total=report.total,
        )
        return 1

    if ratchet.total_unrecorded(report, baseline):
        print(
            f"{BASELINE_NAME} is out of date: total coverage is "
            f"{report.total:.2f}%, but only {baseline['total']:.2f}% is "
            f"recorded.\n"
        )
        print(_how_to_record("Record"))
        return 1

    return 0


def check_ratchet(config: Config, *, update: bool) -> int:
    """Apply the per-file coverage gate.

    Args:
        config: The resolved project configuration.
        update: Whether to raise the baseline on success.

    Returns:
        0 if coverage held or improved, 1 otherwise.
    """
    report: Report = ratchet.read_report(config.coverage_json, config.exclude)
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
    elif (code := _check_staleness(config, report, baseline)) != 0:
        return code
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


def _staged_diff(config: Config) -> str:
    """Read the diff of what is about to be committed.

    Args:
        config: The resolved project configuration.

    Returns:
        A unified diff with no context, empty if git had nothing to say.
    """
    completed = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color", "--relative"],
        cwd=config.root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


def _mutmut_verdicts(config: Config) -> dict[str, str]:
    """Read the verdict mutmut currently records against each mutant.

    A failure here is not worth reporting: the verdicts are an optimisation,
    and without them every selected function is simply tested again.

    Args:
        config: The resolved project configuration.

    Returns:
        A verdict per mutant name, empty if none could be read.
    """
    code, output = run_uv_captured(["mutmut", "results", "--all", "True"], config)
    return mutants.parse_verdicts(output) if code == 0 else {}


def _discard_test_associations(config: Config) -> None:
    """Make mutmut work out afresh which tests reach which functions.

    It caches that map and rebuilds it only for tests it has not seen before.
    Editing a function invalidates its entry without any test being new, so the
    function is left with no tests to kill its mutants and every one of them
    survives - reported against the code you just changed, which is the one
    place a false survivor does the most damage.

    Rebuilding costs one instrumented run of the suite, so callers only pay it
    when a mutant is actually going to be tested.

    Args:
        config: The resolved project configuration.
    """
    (config.root / "mutants" / MUTMUT_STATS_NAME).unlink(missing_ok=True)


def _print_survivors(rows: Sequence[mutants.Summary]) -> None:
    """Report each function whose mutants got through, worst first.

    Laid out like the regression table: which of several functions to write an
    assertion for first is the question, so the counts have to be comparable at
    a glance.
    """
    path_width = max(max(len(row.path) for row in rows), len("FILE"))
    name_width = max(max(len(row.function) for row in rows), len("FUNCTION"))

    print("Mutants survived in the code you are committing\n")
    print(
        f"  {'FILE':<{path_width}}  {'FUNCTION':<{name_width}}  "
        f"{'SURVIVED':>8}  {'MUTANTS':>7}"
    )
    print(f"  {'─' * (path_width + name_width + 23)}")
    for row in rows:
        print(
            f"  {row.path:<{path_width}}  "
            f"{row.function:<{name_width}}  "
            f"{row.survived:>8}  "
            f"{row.total:>7}"
        )
    print(
        "\n  Advisory only - a surviving mutant is evidence the tests do not\n"
        "  distinguish a behaviour, not proof of a bug.\n"
    )
    print("  Review survivors with: uv run mutmut browse")


def _run_staged_tests(config: Config, paths: Sequence[str]) -> int:
    """Run the test modules this commit edits.

    mutmut mutates the code under test, never the tests themselves, so a commit
    that only touches a test file selects no mutants and the mutation pass
    would report nothing at all. Running the modules you staged closes that.

    Args:
        config: The resolved project configuration.
        paths: The staged test modules.

    Returns:
        0 if they passed, 1 otherwise.
    """
    status(
        f"Running {len(paths)} staged test {'module' if len(paths) == 1 else 'modules'}..."
    )
    code, output = run_uv_captured(["pytest", *paths], config)
    # An empty module is not a broken one, and is a normal state to commit
    # while writing the first test in a file.
    if code in (0, PYTEST_NO_TESTS_COLLECTED):
        return 0
    fail("Staged tests failed:")
    print(output)
    return 1


def run_commit_checks(config: Config) -> int:
    """Check the code this commit changes, before it becomes a commit.

    Two things, in the order that makes the second meaningful. The tests
    reaching the changed code have to pass, which gates: a failing test is not
    a judgement call. Then the changed functions are mutation-tested, which
    does not gate, for the reason set out in run_mutation_testing.

    The tests are mostly not run here. mutmut rebuilds its map of which test
    reaches which function, selects exactly those for the functions being
    mutated, runs them, and stops if any fail - so the gate is to report that
    rather than swallow it. What is run here is the test modules the commit
    edits, which mutmut has no reason to look at.

    Everything is narrowed to the staged diff, because the whole tree is too
    much to ask of a commit and the code in front of you is the code you can
    still do something about. Completeness belongs to the pre-push gates, which
    run the suite entire.

    Args:
        config: The resolved project configuration.

    Returns:
        0 if the commit may proceed, 1 if a test failed.
    """
    changed = mutants.added_lines(_staged_diff(config))

    staged_tests = mutants.test_files(changed)
    if staged_tests and (code := _run_staged_tests(config, staged_tests)) != 0:
        return code

    targets = mutants.select(config.root, changed)
    if not targets:
        status("Mutation testing: no functions changed in this commit")
        return 0

    if not is_installed("mutmut", config):
        warn(
            "mutmut is not installed in this project - skipping mutation testing.\n"
            "    Add it with: uv add --dev mutmut"
        )
        return 0

    verdicts = _mutmut_verdicts(config)
    to_run, settled = mutants.partition(config.root, targets, verdicts)
    if not to_run and not settled:
        status("Mutation testing: nothing mutable in the functions you changed")
        return 0

    if to_run:
        status(
            f"Testing {len(to_run)} changed "
            f"{'function' if len(to_run) == 1 else 'functions'}..."
        )
        _discard_test_associations(config)
        # Captured rather than inherited: this runs as a hook that has to print
        # unconditionally to be seen at all, and mutmut's progress spinners on
        # every commit would drown the one table worth reading.
        code, noise = run_uv_captured(
            ["mutmut", "run", *(target.glob for target in to_run)], config
        )
        if code != 0:
            # Survivors alone leave mutmut's status at zero, so a non-zero one
            # means it could not do its job. Overwhelmingly that is the tests
            # it runs first against unmutated code having failed.
            fail("Tests covering the changed code failed:")
            print(mutants.without_progress(noise))
            return 1
        verdicts = _mutmut_verdicts(config)
        if not verdicts:
            # Claiming nothing survived would be a lie about a run we cannot
            # read the results of.
            warn("mutmut ran but reported no results:")
            print(mutants.without_progress(noise))
            return 0

    rows = mutants.summarise([*to_run, *settled], verdicts)
    if not rows:
        status("No mutants survived in the code you are committing")
        return 0
    print()
    _print_survivors(rows)
    return 0


def run_mutation_testing(config: Config) -> None:
    """Run mutmut over the whole tree and point the user at the survivor list.

    Mutation testing is advisory: a surviving mutant is evidence the tests do
    not distinguish a behaviour, not proof of a bug, and gating on the score
    encourages writing assertions that kill mutants instead of assertions that
    describe behaviour. Nothing here fails the run.

    Nothing needs to be ignored to achieve that, because mutmut exits zero
    however many mutants survive. A missing mutmut would still look exactly
    like a successful sweep, though - hence the explicit check first.

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
