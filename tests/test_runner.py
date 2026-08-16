"""Tests for the orchestration layer.

The gate decisions live here, so these tests cover which subprocesses get run,
with which arguments, and what exit status each gate produces. Subprocess
execution itself is stubbed - the point is the decisions, not uv's behaviour.
"""

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from proofmark import runner
from proofmark.config import Config
from proofmark.ratchet import Regression, Unrecorded


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        root=tmp_path,
        source="mypkg",
        diff_threshold=80,
        compare_branch="origin/main",
    )


# A denominator large enough to reproduce these percentages exactly.
SCALE = 100_000


def write_report(
    config: Config, *, total: float, measured: int, files: dict[str, float]
) -> None:
    config.coverage_json.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": total,
                    "num_statements": measured,
                    "num_branches": 0,
                },
                "files": {
                    name: {
                        "summary": {
                            "covered_lines": round(SCALE * pct / 100),
                            "num_statements": SCALE,
                        }
                    }
                    for name, pct in files.items()
                },
            }
        )
    )


def write_baseline_json(
    config: Config, *, total: float, measured: int, files: dict[str, float]
) -> None:
    """Write a baseline in the committed form, from percentages a test can read."""
    config.baseline.write_text(
        json.dumps(
            {
                "total": total,
                "measured": measured,
                "files": {
                    name: [round(SCALE * (100 - pct) / 100), SCALE]
                    for name, pct in files.items()
                },
            }
        )
    )


class RecordingRun:
    """Captures subprocess invocations instead of executing them."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.envs: list[dict[str, str]] = []
        self.cwds: list[Path] = []

    def __call__(
        self, args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        env = kwargs.get("env")
        if isinstance(env, dict):
            self.envs.append(env)
        cwd = kwargs.get("cwd")
        if isinstance(cwd, Path):
            self.cwds.append(cwd)
        return subprocess.CompletedProcess(args=list(args), returncode=self.returncode)


class Commands:
    """Answers each subprocess call according to the tool it invokes.

    The staged mutation pass shells out to several tools in one pass and reads
    the output of some of them, so a single canned reply will not do.
    """

    def __init__(
        self,
        outputs: dict[str, str],
        returncode: int = 0,
        failing: Sequence[str] = (),
    ) -> None:
        self.outputs = outputs
        self.returncode = returncode
        self.failing = failing
        self.calls: list[list[str]] = []
        self.kwargs_by_call: dict[str, dict[str, object]] = {}

    def __call__(
        self, args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        self.kwargs_by_call[" ".join(args)] = kwargs
        stdout = next((out for token, out in self.outputs.items() if token in args), "")
        failed = any(token in args for token in self.failing)
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=1 if failed else self.returncode,
            stdout=stdout,
        )

    def invoked(self, token: str) -> list[list[str]]:
        """Every call that ran the named tool."""
        return [call for call in self.calls if token in call]


class FakeStdout:
    """Stands in for sys.stdout with a controllable isatty()."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def flush(self) -> None:
        return None


# Colour handling


def test_colour_is_applied_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout", FakeStdout(tty=True))

    assert runner._colour(runner.GREEN, "ok") == f"{runner.GREEN}ok{runner.RESET}"


def test_colour_is_stripped_when_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escape codes must not leak into piped output or captured hook logs."""
    monkeypatch.setattr("sys.stdout", FakeStdout(tty=False))

    assert runner._colour(runner.GREEN, "ok") == "ok"


# Subprocess environment


def test_uv_environment_drops_virtual_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook's isolated venv sets VIRTUAL_ENV, which makes uv warn on every call.

    uv resolves the project environment from the working directory either way,
    so removing the variable only quietens the output.
    """
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")

    assert "VIRTUAL_ENV" not in runner._env_for_uv()


def test_uv_environment_preserves_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_TOKEN", "keep-me")

    assert runner._env_for_uv()["SOME_TOKEN"] == "keep-me"


def test_commands_run_in_the_project_root(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_uv(["echo", "hi"], config)

    assert recorder.calls == [["uv", "run", "echo", "hi"]]
    assert recorder.cwds == [config.root]


def test_run_uv_propagates_exit_status(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=3))

    assert runner.run_uv(["false"], config) == 3


def test_run_uv_does_not_raise_on_failure(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """A failing gate must return its status, not blow up with a traceback.

    With check=True a failing pytest would surface as CalledProcessError
    instead of a clean non-zero exit from proofmark.
    """
    recorder = RecordingRun(returncode=1)
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_uv(["false"], config)

    assert recorder.kwargs[0]["check"] is False


def test_run_uv_passes_the_cleaned_environment(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Stripping VIRTUAL_ENV only helps if the cleaned env actually gets used."""
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_uv(["true"], config)

    assert "VIRTUAL_ENV" not in recorder.envs[0]


# pytest invocation


def test_pytest_is_invoked_with_both_report_formats(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """JSON feeds the per-file ratchet; Cobertura XML is all diff-cover reads."""
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_pytest(config)

    (call,) = recorder.calls
    assert "--cov-report=json" in call
    assert "--cov-report=xml" in call


def test_pytest_measures_the_configured_source(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_pytest(config)

    assert "--cov=mypkg" in recorder.calls[0]
    assert "--cov-branch" in recorder.calls[0]


def test_pytest_is_the_command_that_runs(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_pytest(config)

    assert recorder.calls[0][:3] == ["uv", "run", "pytest"]


# Ratchet gate


def test_gate_passes_when_coverage_holds(config: Config) -> None:
    write_report(config, total=50.0, measured=100, files={"a.py": 50.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})

    assert runner.check_ratchet(config, update=False) == 0


def test_gate_fails_on_a_per_file_drop(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    write_report(config, total=50.0, measured=100, files={"a.py": 10.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 90.0})

    assert runner.check_ratchet(config, update=False) == 1
    assert "a.py" in capsys.readouterr().out


def test_gate_fails_on_a_total_drop(config: Config) -> None:
    write_report(config, total=10.0, measured=100, files={})
    write_baseline_json(config, total=90.0, measured=100, files={})

    assert runner.check_ratchet(config, update=False) == 1


def test_check_mode_leaves_the_baseline_untouched(config: Config) -> None:
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})
    original = config.baseline.read_text()

    runner.check_ratchet(config, update=False)

    assert config.baseline.read_text() == original


def test_update_mode_raises_the_baseline(config: Config) -> None:
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})

    runner.check_ratchet(config, update=True)

    missing, measured = json.loads(config.baseline.read_text())["files"]["a.py"]
    assert 100.0 * (measured - missing) / measured == 90.0


def test_update_mode_seeds_a_missing_baseline(config: Config) -> None:
    write_report(config, total=42.0, measured=100, files={"a.py": 42.0})

    assert runner.check_ratchet(config, update=True) == 0
    assert config.baseline.exists()


def test_gate_ignores_excluded_paths(tmp_path: Path) -> None:
    """The test suite's own coverage must not be folded into the project's."""
    config = Config(
        root=tmp_path,
        source=".",
        diff_threshold=80,
        compare_branch="origin/main",
        exclude=("tests",),
    )
    config.coverage_json.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 75.0,
                    "num_statements": 200,
                    "num_branches": 0,
                },
                "files": {
                    "app.py": {
                        "summary": {
                            "percent_covered": 50.0,
                            "covered_lines": 50,
                            "num_statements": 100,
                        }
                    },
                    "tests/test_app.py": {
                        "summary": {
                            "percent_covered": 100.0,
                            "covered_lines": 100,
                            "num_statements": 100,
                        }
                    },
                },
            }
        )
    )

    assert runner.check_ratchet(config, update=True) == 0

    written = json.loads(config.baseline.read_text())
    assert list(written["files"]) == ["app.py"]
    assert written["total"] == 50.0
    assert written["measured"] == 100


# Staleness gate
#
# The hook runs --check, which never writes. Without this gate a project can
# earn coverage, never commit it, and keep being told "All checks passed"
# while the recorded standard sits at whatever it was first seeded with.


def test_check_mode_fails_on_an_unrecorded_gain(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})

    assert runner.check_ratchet(config, update=False) == 1

    out = capsys.readouterr().out
    assert "a.py" in out
    assert "proofmark run" in out


def test_check_mode_fails_on_an_untracked_file(config: Config) -> None:
    """The case that let actual_data_munging rot: measured but never recorded."""
    write_report(config, total=50.0, measured=100, files={"a.py": 50.0, "new.py": 80.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})

    assert runner.check_ratchet(config, update=False) == 1


def test_check_mode_reports_a_missing_baseline(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listing every file would be noise - the whole project is the gain."""
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})

    assert runner.check_ratchet(config, update=False) == 1

    out = capsys.readouterr().out
    assert "No baseline" in out
    assert "proofmark check --update" in out
    assert "a.py" not in out


def test_check_mode_passes_on_a_current_baseline(config: Config) -> None:
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=90.0, measured=100, files={"a.py": 90.0})

    assert runner.check_ratchet(config, update=False) == 0


def test_check_mode_tolerates_a_deleted_file(config: Config) -> None:
    """A stale entry for removed code is not a gain, and must not fail a push."""
    write_report(config, total=90.0, measured=80, files={"a.py": 90.0})
    write_baseline_json(
        config, total=90.0, measured=100, files={"a.py": 90.0, "gone.py": 100.0}
    )

    assert runner.check_ratchet(config, update=False) == 0


def test_check_mode_tolerates_a_shrinking_codebase(config: Config) -> None:
    """Deleting poorly covered code raises the average without earning it."""
    write_report(config, total=95.0, measured=80, files={"a.py": 90.0})
    write_baseline_json(config, total=90.0, measured=100, files={"a.py": 90.0})

    assert runner.check_ratchet(config, update=False) == 0


def test_a_regression_is_reported_alone(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drop is the more serious finding; the staleness table would bury it."""
    write_report(config, total=50.0, measured=100, files={"a.py": 10.0, "b.py": 99.0})
    write_baseline_json(
        config, total=50.0, measured=100, files={"a.py": 90.0, "b.py": 50.0}
    )

    assert runner.check_ratchet(config, update=False) == 1
    assert "out of date" not in capsys.readouterr().out


def test_check_mode_fails_on_a_total_the_files_do_not_explain(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edited or truncated baseline can hold floors but lose the total.

    Every file is at its floor here, so only the total says the record is
    behind - and it still has to be brought up to date.
    """
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 90.0})

    assert runner.check_ratchet(config, update=False) == 1
    assert "out of date" in capsys.readouterr().out


def test_update_mode_records_instead_of_failing(config: Config) -> None:
    """The writing path answers staleness by fixing it, so it must not gate."""
    write_report(config, total=90.0, measured=100, files={"a.py": 90.0})
    write_baseline_json(config, total=50.0, measured=100, files={"a.py": 50.0})

    assert runner.check_ratchet(config, update=True) == 0


# Regression report


def test_regression_table_aligns_to_the_longest_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner._print_regressions(
        [
            Regression("a/very/long/path/to/a/module.py", 100.0, 10.0),
            Regression("short.py", 90.0, 80.0),
        ]
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if "%" in line]
    assert len({len(line.rstrip()) for line in lines}) == 1


def test_regression_table_shows_the_drop_as_negative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner._print_regressions([Regression("a.py", 90.0, 80.0)])

    out = capsys.readouterr().out
    assert "90.00%" in out
    assert "80.00%" in out
    assert "-10.00" in out


def test_regression_table_names_the_baseline_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner._print_regressions([Regression("a.py", 90.0, 80.0)])

    assert ".coverage-baseline.json" in capsys.readouterr().out


# Unrecorded gain report


def test_unrecorded_table_aligns_to_the_longest_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner._print_unrecorded(
        [
            Unrecorded("a/very/long/path/to/a/module.py", 10.0, 100.0),
            Unrecorded("short.py", 80.0, 90.0),
        ],
        recorded_total=50.0,
        current_total=95.0,
    )

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "%" in line and "Total" not in line
    ]
    assert len({len(line.rstrip()) for line in lines}) == 1


def test_unrecorded_table_shows_the_gain_as_positive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner._print_unrecorded(
        [Unrecorded("a.py", 80.0, 90.0)], recorded_total=80.0, current_total=90.0
    )

    out = capsys.readouterr().out
    assert "80.00%" in out
    assert "90.00%" in out
    assert "+10.00" in out


def test_unrecorded_table_marks_an_untracked_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An untracked file has no floor at all, which is worth saying plainly."""
    runner._print_unrecorded(
        [Unrecorded("new.py", None, 40.0)], recorded_total=0.0, current_total=40.0
    )

    assert "(new)" in capsys.readouterr().out


def test_unrecorded_table_leads_with_the_cheap_remedy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The report on disk is the one these numbers came from.

    Re-running the suite is wasted work, and re-measuring could land on
    numbers other than the ones the table just showed.
    """
    runner._print_unrecorded(
        [Unrecorded("a.py", 80.0, 90.0)], recorded_total=80.0, current_total=90.0
    )

    out = capsys.readouterr().out
    assert "proofmark check --update" in out
    assert out.index("check --update") < out.index("proofmark run")
    assert ".coverage-baseline.json" in out


# Diff coverage gate


def test_diff_coverage_is_skipped_without_the_compare_branch(
    monkeypatch: pytest.MonkeyPatch, config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing base branch is a local condition, not a quality failure."""
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=1))

    assert runner.check_diff_coverage(config) == 0
    assert "skipping diff coverage" in capsys.readouterr().out


def test_diff_coverage_passes_the_threshold_and_branch(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    recorder = RecordingRun(returncode=0)
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.check_diff_coverage(config)

    diff_call = recorder.calls[-1]
    assert "diff-cover" in diff_call
    assert "--compare-branch=origin/main" in diff_call
    assert "--fail-under=80" in diff_call


def test_diff_coverage_propagates_failure(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """The branch exists and diff-cover is present, but the gate itself fails."""
    monkeypatch.setattr(runner, "_compare_branch_exists", lambda config: True)
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=4))

    assert runner.check_diff_coverage(config) == 4


# Mutation testing


def test_mutation_testing_never_gates(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Survivors are information, so nothing about them fails the sweep."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=2))

    runner.run_mutation_testing(config)  # must not raise


def test_mutation_testing_invokes_mutmut(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_mutation_testing(config)

    assert recorder.calls == [["uv", "run", "mutmut", "run"]]


# Mutation testing on the staged diff


STAGED_DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,0 +2,1 @@
+    return value + 1
"""


@pytest.fixture
def staged(config: Config) -> None:
    """Put the file the canned diff refers to on disk."""
    module = config.root / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("def thing(value):\n    return value + 1\n")


def test_staged_mutation_asks_only_for_the_changed_function(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands({"diff": STAGED_DIFF})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert [
        "uv",
        "run",
        "mutmut",
        "run",
        "pkg.mod.x_thing__mutmut_*",
    ] in commands.calls


STAGED_TEST_DIFF = """diff --git a/tests/test_mod.py b/tests/test_mod.py
--- a/tests/test_mod.py
+++ b/tests/test_mod.py
@@ -1,0 +2,1 @@
+    assert thing(1) == 2
"""


def test_a_staged_test_module_is_run(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Mutmut never mutates a test, so nothing else would notice it broke."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands({"diff": STAGED_TEST_DIFF})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert ["uv", "run", "pytest", "tests/test_mod.py"] in commands.calls


def test_a_failing_staged_test_blocks_the_commit(
    monkeypatch: pytest.MonkeyPatch, config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands(
        {"diff": STAGED_TEST_DIFF, "pytest": "E   assert 1 == 2\n"},
        failing=["pytest"],
    )
    monkeypatch.setattr(subprocess, "run", commands)

    assert runner.run_commit_checks(config) == 1

    out = capsys.readouterr().out
    assert "assert 1 == 2" in out
    assert commands.invoked("mutmut") == []


def test_a_test_module_that_collects_nothing_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Pytest exits 5 for an empty module, which is not a broken test."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands({"diff": STAGED_TEST_DIFF}, returncode=5)
    monkeypatch.setattr(subprocess, "run", commands)

    assert runner.run_commit_checks(config) == 0


def test_mutmut_failing_to_complete_blocks_the_commit(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It runs the tests reaching the changed functions and stops if they fail.

    Its status is only non-zero when it could not do its job - survivors alone
    leave it zero - so this is the tests, reported rather than swallowed.
    """
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands(
        {"diff": STAGED_DIFF, "run": "Failed to run clean test\n"},
        failing=["mutmut"],
    )
    monkeypatch.setattr(subprocess, "run", commands)

    assert runner.run_commit_checks(config) == 1
    assert "Failed to run clean test" in capsys.readouterr().out


def test_a_clean_commit_reports_success(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    results = "    pkg.mod.x_thing__mutmut_1: killed\n"
    monkeypatch.setattr(
        subprocess, "run", Commands({"diff": STAGED_DIFF, "results": results})
    )

    assert runner.run_commit_checks(config) == 0


def test_the_staged_diff_is_read_without_context_lines(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Context lines would widen the selection to functions nobody touched."""
    commands = Commands({})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    (diff_call,) = commands.invoked("diff")
    assert "--cached" in diff_call
    assert "--unified=0" in diff_call


def test_unreadable_verdicts_leave_the_cache_unused(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    """Verdicts are an optimisation; failing to read them must not skip work."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    results = "    pkg.mod.x_thing__mutmut_1: killed\n"
    commands = Commands({"diff": STAGED_DIFF, "results": results}, failing=["results"])
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert ["uv", "run", "mutmut", "run", "pkg.mod.x_thing__mutmut_*"] in commands.calls


def test_mutmuts_own_output_is_kept_out_of_the_way(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hook has to print unconditionally to be seen, so it prints little.

    A passing hook's output is hidden unless it is marked verbose, and a
    verbose hook shows everything - including mutmut's progress spinners on
    every single commit.
    """
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    results = "    pkg.mod.x_thing__mutmut_1: survived\n"
    commands = Commands({"diff": STAGED_DIFF, "results": results})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    mutation_run = "uv run mutmut run pkg.mod.x_thing__mutmut_*"
    assert commands.kwargs_by_call[mutation_run]["capture_output"] is True


def test_a_mutmut_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Quietening mutmut must not hide it falling over."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        Commands({"diff": STAGED_DIFF, "run": "AssertionError: boom\n"}),
    )

    runner.run_commit_checks(config)

    assert "AssertionError: boom" in capsys.readouterr().out


def test_stale_test_associations_are_discarded_before_running(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    """Editing a function strips mutmut's record of which tests reach it.

    Its stats cache is rebuilt only for tests that are new, so an existing test
    covering a function you just changed is never re-associated with it, and
    every mutant of that function then survives for want of anyone to kill it.
    Those survivors are not real, and a report full of them is worse than no
    report at all.
    """
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    stats = config.root / "mutants" / "mutmut-stats.json"
    stats.parent.mkdir(parents=True)
    stats.write_text("{}")
    monkeypatch.setattr(subprocess, "run", Commands({"diff": STAGED_DIFF}))

    runner.run_commit_checks(config)

    assert not stats.exists()


def test_associations_are_kept_when_nothing_needs_running(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    """Only pay for the rebuild when a mutant is going to be tested.

    It costs a whole instrumented run of the suite.
    """
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    copy = config.root / "mutants" / "pkg" / "mod.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("generated\n")
    os.utime(config.root / "pkg" / "mod.py", (0, 100))
    os.utime(copy, (0, 200))
    stats = config.root / "mutants" / "mutmut-stats.json"
    stats.write_text("{}")
    results = "    pkg.mod.x_thing__mutmut_1: killed\n"
    monkeypatch.setattr(
        subprocess, "run", Commands({"diff": STAGED_DIFF, "results": results})
    )

    runner.run_commit_checks(config)

    assert stats.exists()


def test_a_change_with_nothing_to_mutate_stops_before_running(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutmut errors on a pattern matching nothing, so it must not be asked."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    copy = config.root / "mutants" / "pkg" / "mod.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("generated\n")
    os.utime(config.root / "pkg" / "mod.py", (0, 100))
    os.utime(copy, (0, 200))
    elsewhere = "    other.x_thing__mutmut_1: killed\n"
    commands = Commands({"diff": STAGED_DIFF, "results": elsewhere})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert [call for call in commands.calls if call[-1].endswith("__mutmut_*")] == []
    assert "nothing mutable" in capsys.readouterr().out


def test_nothing_staged_leaves_mutmut_alone(
    monkeypatch: pytest.MonkeyPatch, config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit that touches no function should cost nothing at all."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    commands = Commands({})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert commands.invoked("mutmut") == []
    assert "no functions" in capsys.readouterr().out


def test_staged_mutation_never_gates(
    monkeypatch: pytest.MonkeyPatch, config: Config, staged: None
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    monkeypatch.setattr(
        subprocess, "run", Commands({"diff": STAGED_DIFF}, returncode=2)
    )

    runner.run_commit_checks(config)  # must not raise


def test_survivors_in_the_staged_function_are_reported(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    results = (
        "    pkg.mod.x_thing__mutmut_1: killed\n"
        "    pkg.mod.x_thing__mutmut_2: survived\n"
    )
    monkeypatch.setattr(
        subprocess, "run", Commands({"diff": STAGED_DIFF, "results": results})
    )

    runner.run_commit_checks(config)

    out = capsys.readouterr().out
    assert "pkg/mod.py" in out
    assert "thing" in out
    assert "Advisory" in out


def test_a_settled_function_is_reported_without_being_rerun(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutmut re-tests anything named explicitly, so the skipping is ours."""
    monkeypatch.setattr(runner, "is_installed", lambda module, config: True)
    copy = config.root / "mutants" / "pkg" / "mod.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("generated\n")
    os.utime(config.root / "pkg" / "mod.py", (0, 100))
    os.utime(copy, (0, 200))
    results = "    pkg.mod.x_thing__mutmut_1: survived\n"
    commands = Commands({"diff": STAGED_DIFF, "results": results})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert [call for call in commands.calls if call[-1].endswith("__mutmut_*")] == []
    assert "thing" in capsys.readouterr().out


def test_missing_mutmut_skips_the_staged_pass(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    staged: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner, "is_installed", lambda module, config: False)
    commands = Commands({"diff": STAGED_DIFF})
    monkeypatch.setattr(subprocess, "run", commands)

    runner.run_commit_checks(config)

    assert "mutmut is not installed" in capsys.readouterr().out
    assert commands.invoked("mutmut") == []


# Missing optional tooling
#
# proofmark declares no dependencies and resolves these from the project being
# checked, so absence has to produce a clear message rather than a confusing
# failure or a silent no-op.


def test_missing_mutmut_is_reported_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch, config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutmut's exit status is ignored, so absence must be detected up front.

    Otherwise a project without mutmut would see proofmark report success
    having run no mutation testing at all.
    """
    monkeypatch.setattr(runner, "is_installed", lambda module, config: False)
    recorder = RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)

    runner.run_mutation_testing(config)

    out = capsys.readouterr().out
    assert "mutmut is not installed" in out
    assert "uv add --dev mutmut" in out
    assert recorder.calls == []


def test_missing_diff_cover_fails_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "_compare_branch_exists", lambda config: True)
    monkeypatch.setattr(runner, "is_installed", lambda module, config: False)

    assert runner.check_diff_coverage(config) == 1
    assert "uv add --dev diff-cover" in capsys.readouterr().out


def test_is_installed_probes_the_project_environment(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    recorder = RecordingRun(returncode=0)
    monkeypatch.setattr(subprocess, "run", recorder)

    assert runner.is_installed("somemodule", config) is True
    assert recorder.calls == [["uv", "run", "python", "-c", "import somemodule"]]
    assert recorder.cwds == [config.root]


def test_is_installed_reports_false_on_import_failure(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=1))

    assert runner.is_installed("missing", config) is False
