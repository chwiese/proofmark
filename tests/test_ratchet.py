"""Tests for the coverage ratchet.

The behaviours pinned here are the ones that make the ratchet trustworthy: it
must catch real regressions, must never quietly lower a floor, and must not
punish deleting code.
"""

import json
from pathlib import Path

import pytest

from proofmark.ratchet import (
    EPSILON,
    Baseline,
    RatchetError,
    Report,
    find_regressions,
    find_unrecorded,
    raise_baseline,
    read_baseline,
    read_report,
    total_regressed,
    total_unrecorded,
    write_baseline,
)


def make_report(
    files: dict[str, float] | None = None,
    *,
    total: float = 50.0,
    measured: int = 1000,
) -> Report:
    return Report(total=total, measured=measured, files=files or {})


def make_baseline(
    files: dict[str, float] | None = None,
    *,
    total: float = 50.0,
    measured: int = 1000,
) -> Baseline:
    return {"total": total, "measured": measured, "files": files or {}}


def write_coverage_json(
    path: Path,
    *,
    total: float,
    statements: int,
    branches: int,
    files: dict[str, float],
) -> None:
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": total,
                    "num_statements": statements,
                    "num_branches": branches,
                },
                "files": {
                    name: {"summary": {"percent_covered": pct}}
                    for name, pct in files.items()
                },
            }
        )
    )


def write_counted_coverage_json(path: Path, files: dict[str, tuple[int, int]]) -> None:
    """Write a report whose per-file summaries carry real counts.

    `files` maps a path to (covered, measured), each already combining
    statements and branches - the denominator coverage.py reports percentages
    against. The totals block is filled in consistently across every file, so
    a test that excludes one can tell a recomputed total from a reused one.
    """
    summaries = {
        name: {
            "covered_lines": covered,
            "num_statements": measured,
            "covered_branches": 0,
            "num_branches": 0,
            "percent_covered": 100.0 * covered / measured if measured else 100.0,
        }
        for name, (covered, measured) in files.items()
    }
    covered_total = sum(covered for covered, _ in files.values())
    measured_total = sum(measured for _, measured in files.values())
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": (
                        100.0 * covered_total / measured_total
                        if measured_total
                        else 100.0
                    ),
                    "num_statements": measured_total,
                    "num_branches": 0,
                },
                "files": {
                    name: {"summary": summary} for name, summary in summaries.items()
                },
            }
        )
    )


# Reading reports


def test_read_report_extracts_totals_and_files(tmp_path: Path) -> None:
    report_path = tmp_path / "coverage.json"
    write_coverage_json(
        report_path, total=42.5, statements=200, branches=50, files={"a.py": 80.0}
    )

    report = read_report(report_path)

    assert report.total == 42.5
    assert report.measured == 250  # statements + branches
    assert report.files == {"a.py": 80.0}


def test_read_report_handles_a_report_without_branch_data(tmp_path: Path) -> None:
    """coverage.py omits the branch keys when branch coverage is not in effect.

    A project measuring statements only still produces a usable report, so
    the missing key counts as zero branches rather than an error.
    """
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 60.0, "num_statements": 300},
                "files": {"a.py": {"summary": {"percent_covered": 60.0}}},
            }
        )
    )

    report = read_report(path)

    assert report.measured == 300


def test_read_report_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RatchetError, match="not found"):
        read_report(tmp_path / "nope.json")


def test_read_report_rejects_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text('{"totals": {}}')

    with pytest.raises(RatchetError, match="could not parse"):
        read_report(path)


# Excluding paths from the report
#
# A flat-module project is measured as `.`, which sweeps the test suite in
# alongside the code. Those files belong to neither gate: pinning a test file
# at a coverage floor records nothing anyone would act on.


def test_excluded_files_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    write_counted_coverage_json(
        path, {"app.py": (50, 100), "tests/test_app.py": (100, 100)}
    )

    report = read_report(path, exclude=["tests"])

    assert set(report.files) == {"app.py"}


def test_excluding_recomputes_the_total_and_measured_size(tmp_path: Path) -> None:
    """The totals block still counts the excluded files, so it cannot be reused.

    Left alone it would report the test suite's own coverage as part of the
    project's, which is the number the baseline then locks in.
    """
    path = tmp_path / "coverage.json"
    write_counted_coverage_json(
        path, {"app.py": (50, 100), "tests/test_app.py": (100, 100)}
    )

    report = read_report(path, exclude=["tests"])

    assert report.total == pytest.approx(50.0)  # not 75.0, the two-file average
    assert report.measured == 100


def test_totals_are_used_verbatim_when_nothing_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    write_counted_coverage_json(
        path, {"app.py": (50, 100), "tests/test_app.py": (100, 100)}
    )

    report = read_report(path, exclude=["nowhere"])

    assert report.total == pytest.approx(75.0)
    assert report.measured == 200


def test_a_prefix_matches_only_whole_path_segments(tmp_path: Path) -> None:
    """`tests` must not swallow `tests_helper.py`, which is ordinary source."""
    path = tmp_path / "coverage.json"
    write_counted_coverage_json(
        path, {"tests_helper.py": (1, 2), "tests/test_app.py": (1, 1)}
    )

    report = read_report(path, exclude=["tests"])

    assert set(report.files) == {"tests_helper.py"}


def test_excluding_the_whole_report_measures_nothing(tmp_path: Path) -> None:
    """Nothing measured is not a regression - the total gate goes vacuous."""
    path = tmp_path / "coverage.json"
    write_counted_coverage_json(path, {"tests/test_app.py": (1, 1)})

    report = read_report(path, exclude=["tests"])

    assert report.files == {}
    assert report.measured == 0
    assert report.total == 100.0  # coverage.py's own reading of "nothing to cover"


# Per-file gate


def test_no_regression_when_coverage_holds() -> None:
    assert (
        find_regressions(make_report({"a.py": 80.0}), make_baseline({"a.py": 80.0}))
        == []
    )


def test_no_regression_when_coverage_improves() -> None:
    assert (
        find_regressions(make_report({"a.py": 95.0}), make_baseline({"a.py": 80.0}))
        == []
    )


def test_detects_a_dropped_file() -> None:
    (regression,) = find_regressions(
        make_report({"a.py": 60.0}), make_baseline({"a.py": 80.0})
    )

    assert regression.path == "a.py"
    assert regression.baseline == 80.0
    assert regression.current == 60.0
    assert regression.drop == pytest.approx(20.0)


def test_regressions_are_reported_worst_first() -> None:
    report = make_report({"small.py": 78.0, "big.py": 30.0})
    baseline = make_baseline({"small.py": 80.0, "big.py": 90.0})

    paths = [item.path for item in find_regressions(report, baseline)]

    assert paths == ["big.py", "small.py"]


def test_new_files_cannot_regress() -> None:
    """Untracked files are governed by the diff coverage gate, not this one."""
    report = make_report({"a.py": 80.0, "brand_new.py": 0.0})
    baseline = make_baseline({"a.py": 80.0})

    assert find_regressions(report, baseline) == []


def test_deleted_files_drop_out_of_the_baseline() -> None:
    report = make_report({"a.py": 80.0})
    baseline = make_baseline({"a.py": 80.0, "removed.py": 100.0})

    assert find_regressions(report, baseline) == []


def test_float_noise_is_tolerated() -> None:
    """A sub-epsilon wobble is representation noise, not a regression."""
    report = make_report({"a.py": 79.999})
    baseline = make_baseline({"a.py": 80.0})

    assert find_regressions(report, baseline) == []


def test_exactly_one_epsilon_below_is_not_a_regression() -> None:
    """Pins the comparison as strict, at the exact tolerance boundary.

    The boundary is computed rather than written as a literal so the test does
    not depend on how 80.0 - 0.01 rounds in binary floating point.
    """
    floor = 80.0
    report = make_report({"a.py": floor - EPSILON})
    baseline = make_baseline({"a.py": floor})

    assert find_regressions(report, baseline) == []


def test_just_beyond_the_tolerance_is_a_regression() -> None:
    floor = 80.0
    report = make_report({"a.py": floor - EPSILON * 2})
    baseline = make_baseline({"a.py": floor})

    assert len(find_regressions(report, baseline)) == 1


# Total gate


def test_total_regression_is_caught_at_equal_size() -> None:
    report = make_report(total=40.0, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    assert total_regressed(report, baseline)


def test_total_regression_ignored_when_codebase_shrank() -> None:
    """Deleting well-covered code lowers the average without anything worsening.

    A file at 100% inside a project at 32% drags the mean down when removed.
    Gating on that would penalise dead-code removal and ordinary refactoring.
    """
    report = make_report(total=40.0, measured=800)
    baseline = make_baseline(total=60.0, measured=1000)

    assert not total_regressed(report, baseline)


def test_total_regression_still_caught_when_codebase_grew() -> None:
    report = make_report(total=40.0, measured=1200)
    baseline = make_baseline(total=60.0, measured=1000)

    assert total_regressed(report, baseline)


def test_total_improvement_is_not_a_regression() -> None:
    report = make_report(total=70.0, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    assert not total_regressed(report, baseline)


def test_total_exactly_one_epsilon_below_is_not_a_regression() -> None:
    """Pins the total comparison as strict, at the exact tolerance boundary."""
    floor = 60.0
    report = make_report(total=floor - EPSILON, measured=1000)
    baseline = make_baseline(total=floor, measured=1000)

    assert not total_regressed(report, baseline)


def test_equal_size_is_not_treated_as_shrinking() -> None:
    """An unchanged codebase must still be gated on the total.

    If equality counted as shrinking, the total floor would be skipped for the
    most common case of all and coverage could drift down unnoticed.
    """
    report = make_report(total=40.0, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    assert total_regressed(report, baseline)


def test_shrinking_is_not_a_loophole_for_per_file_drops() -> None:
    """The safety property behind skipping the total check when code shrinks."""
    report = make_report({"a.py": 10.0}, total=40.0, measured=800)
    baseline = make_baseline({"a.py": 90.0}, total=60.0, measured=1000)

    assert not total_regressed(report, baseline)
    assert find_regressions(report, baseline)  # per-file gate still fires


# Unrecorded gains
#
# The mirror image of a regression: coverage the project has earned but never
# committed. Left undetected it is the broken ratchet - the gain is not
# protected by anything, and giving it back later trips no gate.


def test_a_raised_file_is_unrecorded() -> None:
    (gain,) = find_unrecorded(
        make_report({"a.py": 90.0}), make_baseline({"a.py": 80.0})
    )

    assert gain.path == "a.py"
    assert gain.baseline == 80.0
    assert gain.current == 90.0
    assert gain.gain == pytest.approx(10.0)


def test_an_untracked_file_is_unrecorded() -> None:
    """A file absent from the baseline has no floor, so it can never regress."""
    (gain,) = find_unrecorded(make_report({"new.py": 40.0}), make_baseline())

    assert gain.path == "new.py"
    assert gain.baseline is None
    assert gain.gain == pytest.approx(40.0)


def test_unrecorded_gains_are_reported_biggest_first() -> None:
    report = make_report({"small.py": 82.0, "big.py": 95.0})
    baseline = make_baseline({"small.py": 80.0, "big.py": 30.0})

    paths = [item.path for item in find_unrecorded(report, baseline)]

    assert paths == ["big.py", "small.py"]


def test_a_held_file_is_not_unrecorded() -> None:
    assert (
        find_unrecorded(make_report({"a.py": 80.0}), make_baseline({"a.py": 80.0}))
        == []
    )


def test_a_regressed_file_is_not_unrecorded() -> None:
    assert (
        find_unrecorded(make_report({"a.py": 60.0}), make_baseline({"a.py": 80.0}))
        == []
    )


def test_float_noise_is_not_an_unrecorded_gain() -> None:
    """Same tolerance as the regression gate, so a wobble cannot fail a push."""
    ceiling = 80.0
    report = make_report({"a.py": ceiling + EPSILON})
    baseline = make_baseline({"a.py": ceiling})

    assert find_unrecorded(report, baseline) == []


def test_just_beyond_the_tolerance_is_an_unrecorded_gain() -> None:
    ceiling = 80.0
    report = make_report({"a.py": ceiling + EPSILON * 2})
    baseline = make_baseline({"a.py": ceiling})

    assert len(find_unrecorded(report, baseline)) == 1


def test_a_deleted_file_is_not_an_unrecorded_gain() -> None:
    """Removing code leaves a stale entry, but nothing was earned by it.

    Failing here would block a push for a pure deletion, which no gate is
    protecting anything by doing.
    """
    report = make_report({"a.py": 80.0})
    baseline = make_baseline({"a.py": 80.0, "gone.py": 100.0})

    assert find_unrecorded(report, baseline) == []


def test_total_gain_is_unrecorded_at_equal_size() -> None:
    report = make_report(total=70.0, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    assert total_unrecorded(report, baseline)


def test_total_gain_ignored_when_codebase_shrank() -> None:
    """Deleting poorly covered code raises the average without earning anything.

    The mirror of the same allowance in total_regressed: once the codebase has
    shrunk, the two totals describe different things and comparing them says
    nothing.
    """
    report = make_report(total=70.0, measured=800)
    baseline = make_baseline(total=60.0, measured=1000)

    assert not total_unrecorded(report, baseline)


def test_a_held_total_is_not_unrecorded() -> None:
    report = make_report(total=60.0, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    assert not total_unrecorded(report, baseline)


def test_total_exactly_one_epsilon_above_is_not_unrecorded() -> None:
    ceiling = 60.0
    report = make_report(total=ceiling + EPSILON, measured=1000)
    baseline = make_baseline(total=ceiling, measured=1000)

    assert not total_unrecorded(report, baseline)


# Raising the baseline


def test_baseline_records_improvements() -> None:
    report = make_report({"a.py": 95.0}, total=70.0, measured=1000)
    baseline = make_baseline({"a.py": 80.0}, total=60.0, measured=1000)

    updated = raise_baseline(report, baseline)

    assert updated["files"]["a.py"] == 95.0
    assert updated["total"] == 70.0


def test_baseline_never_lowers_a_file() -> None:
    """The fix for the 'broken ratchet': an unrecorded gain can be given back."""
    report = make_report({"a.py": 70.0}, total=60.0, measured=1000)
    baseline = make_baseline({"a.py": 80.0}, total=60.0, measured=1000)

    updated = raise_baseline(report, baseline)

    assert updated["files"]["a.py"] == 80.0


def test_baseline_adopts_lower_total_when_codebase_shrank() -> None:
    """Total and measured size are a matched pair describing one snapshot.

    Keeping a stale high total against a newly smaller codebase would
    reintroduce the false positive that total_regressed exists to avoid.
    """
    report = make_report(total=55.0, measured=800)
    baseline = make_baseline(total=60.0, measured=1000)

    updated = raise_baseline(report, baseline)

    assert updated["total"] == 55.0
    assert updated["measured"] == 800


def test_baseline_keeps_higher_total_when_codebase_grew() -> None:
    report = make_report(total=55.0, measured=1200)
    baseline = make_baseline(total=60.0, measured=1000)

    updated = raise_baseline(report, baseline)

    assert updated["total"] == 60.0


def test_baseline_drops_deleted_files() -> None:
    report = make_report({"a.py": 80.0})
    baseline = make_baseline({"a.py": 80.0, "gone.py": 100.0})

    updated = raise_baseline(report, baseline)

    assert "gone.py" not in updated["files"]


def test_baseline_adds_new_files() -> None:
    report = make_report({"a.py": 80.0, "new.py": 40.0})
    baseline = make_baseline({"a.py": 80.0})

    updated = raise_baseline(report, baseline)

    assert updated["files"]["new.py"] == 40.0


def test_a_brand_new_uncovered_file_gets_a_zero_floor() -> None:
    """A new file must be recorded at its real coverage, not a non-zero floor.

    Any floor above the file's actual coverage would fail the very next run,
    for a file nobody had touched.
    """
    report = make_report({"untested.py": 0.0})
    baseline = make_baseline()

    updated = raise_baseline(report, baseline)

    assert updated["files"]["untested.py"] == 0.0


def test_baseline_keeps_the_higher_total_at_equal_size() -> None:
    """Equal size is not shrinking, so the recorded total must not drop.

    This is the slow-leak case: a dip too small to trip the gate would
    otherwise be written into the baseline and become the new normal.
    """
    report = make_report(total=59.999, measured=1000)
    baseline = make_baseline(total=60.0, measured=1000)

    updated = raise_baseline(report, baseline)

    assert updated["total"] == 60.0


# Persistence


def test_missing_baseline_reads_as_empty(tmp_path: Path) -> None:
    baseline = read_baseline(tmp_path / "absent.json")

    assert baseline == {"total": 0.0, "measured": 0, "files": {}}


def test_partial_baseline_falls_back_to_zeroes(tmp_path: Path) -> None:
    """A hand-edited or truncated baseline must not crash the gate.

    Treating absent keys as zero floors is the safe reading: it can only make
    the gate more permissive for that one run, and the next update restores
    the real numbers.
    """
    path = tmp_path / ".coverage-baseline.json"
    path.write_text(json.dumps({"files": {"a.py": 50.0}}))

    baseline = read_baseline(path)

    assert baseline["total"] == 0.0
    assert baseline["measured"] == 0
    assert baseline["files"] == {"a.py": 50.0}


def test_baseline_without_a_files_key_reads_as_empty(tmp_path: Path) -> None:
    """Every key is optional, including the file map itself."""
    path = tmp_path / ".coverage-baseline.json"
    path.write_text(json.dumps({"total": 50.0, "measured": 100}))

    assert read_baseline(path)["files"] == {}


def test_baseline_is_written_one_entry_per_line(tmp_path: Path) -> None:
    """The baseline is committed, so it has to diff readably.

    Serialising it onto a single line would make every change look like the
    whole file was rewritten.
    """
    path = tmp_path / ".coverage-baseline.json"
    write_baseline(path, make_baseline({"a.py": 1.0, "b.py": 2.0}))

    assert len(path.read_text().splitlines()) > 5


def test_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / ".coverage-baseline.json"
    original = make_baseline({"b.py": 12.345}, total=61.234, measured=999)

    write_baseline(path, original)
    restored = read_baseline(path)

    assert restored["measured"] == 999
    assert restored["total"] == pytest.approx(61.23)
    assert restored["files"]["b.py"] == pytest.approx(12.35)


def test_baseline_is_written_with_sorted_keys(tmp_path: Path) -> None:
    """Stable ordering keeps the committed diff readable."""
    path = tmp_path / ".coverage-baseline.json"
    write_baseline(path, make_baseline({"z.py": 1.0, "a.py": 2.0, "m.py": 3.0}))

    written = json.loads(path.read_text())

    assert list(written["files"]) == ["a.py", "m.py", "z.py"]
