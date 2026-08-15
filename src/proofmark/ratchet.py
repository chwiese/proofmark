"""Per-file coverage ratchet.

Compares the per-file coverage percentages in coverage.json against a committed
baseline and reports any file that regressed. In update mode the baseline is
rewritten, raising entries that improved and never lowering one.

Never lowering is the point: if a gain is not recorded, a later change can give
it back unnoticed. That is the "broken ratchet" failure mode, and it is why the
baseline is committed rather than regenerated.

A single global percentage cannot catch a file losing its tests while a new,
well-covered file keeps the total flat - hence per-file tracking.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

# Percentage points of slack, to absorb float representation noise only.
EPSILON = 0.01


class RatchetError(Exception):
    """Raised when coverage data is missing or unreadable."""


class Baseline(TypedDict):
    """Committed coverage floors for one project."""

    total: float
    measured: int
    files: dict[str, float]


@dataclass(frozen=True)
class Report:
    """A parsed coverage.py JSON report."""

    total: float
    # Statements plus branches - the denominator behind percent_covered.
    # Tracked so the total floor can be skipped when the codebase shrinks.
    measured: int
    files: dict[str, float]


@dataclass(frozen=True)
class Regression:
    """One file that fell below its recorded floor."""

    path: str
    baseline: float
    current: float

    @property
    def drop(self) -> float:
        """Percentage points lost."""
        return self.baseline - self.current


@dataclass(frozen=True)
class Unrecorded:
    """One file the baseline understates - a gain nothing is protecting yet."""

    path: str
    # None when the file is absent from the baseline altogether. Such a file
    # has no floor, so the per-file gate can never fail on it.
    baseline: float | None
    current: float

    @property
    def gain(self) -> float:
        """Percentage points above what the baseline records."""
        return self.current - (self.baseline or 0.0)


def _is_excluded(path: str, prefixes: Sequence[str]) -> bool:
    """Test a report path against directory prefixes, whole segments only.

    Matching on a bare string prefix would make `tests` swallow
    `tests_helper.py`, which is ordinary source.
    """
    # coverage.py writes paths with the platform separator.
    normalised = path.replace("\\", "/")
    return any(
        normalised == prefix or normalised.startswith(f"{prefix}/")
        for prefix in prefixes
    )


def _summarise(summaries: Iterable[Mapping[str, float]]) -> tuple[float, int]:
    """Recompute a total and measured size from per-file summaries.

    coverage.py's percentage is covered over measured, counting statements and
    branches together, so summing the per-file numerators and denominators
    reproduces exactly what it would have reported for this subset.

    Args:
        summaries: The `summary` block of each file being counted.

    Returns:
        The percentage covered and the measured size.
    """
    covered = 0
    measured = 0
    for summary in summaries:
        # The branch keys are absent unless branch coverage is in effect.
        covered += int(summary["covered_lines"]) + int(
            summary.get("covered_branches", 0)
        )
        measured += int(summary["num_statements"]) + int(summary.get("num_branches", 0))
    if measured == 0:
        # coverage.py's reading of a project with nothing to cover.
        return 100.0, 0
    return 100.0 * covered / measured, measured


def read_report(path: Path, exclude: Sequence[str] = ()) -> Report:
    """Parse a coverage.py JSON report.

    Args:
        path: Path to coverage.json.
        exclude: Directory prefixes to leave out of the ratchet entirely.

    Returns:
        The parsed report.

    Raises:
        RatchetError: If the report is missing or malformed.
    """
    if not path.exists():
        raise RatchetError(
            f"{path.name} not found - run the test suite with coverage first"
        )

    prefixes = [prefix.replace("\\", "/").rstrip("/") for prefix in exclude]

    try:
        raw = json.loads(path.read_text())
        summaries = {
            name: data["summary"]
            for name, data in raw["files"].items()
            if not _is_excluded(name, prefixes)
        }
        files = {
            name: float(summary["percent_covered"])
            for name, summary in summaries.items()
        }
        if len(summaries) == len(raw["files"]):
            totals = raw["totals"]
            total = float(totals["percent_covered"])
            # coverage.py omits the branch keys entirely when branch coverage
            # is not in effect, so a project measuring statements only is
            # still valid.
            measured = int(totals["num_statements"]) + int(
                totals.get("num_branches", 0)
            )
        else:
            # The totals block still counts the excluded files, so reusing it
            # would fold the test suite's own coverage into the project's.
            total, measured = _summarise(summaries.values())
        return Report(total=total, measured=measured, files=files)
    except (KeyError, TypeError, ValueError) as exc:
        raise RatchetError(f"could not parse {path.name}: {exc}") from exc


def read_baseline(path: Path) -> Baseline:
    """Load the committed baseline, treating a missing file as an empty one.

    Args:
        path: Path to the baseline JSON file.

    Returns:
        The baseline mapping.
    """
    if not path.exists():
        return {"total": 0.0, "measured": 0, "files": {}}

    raw = json.loads(path.read_text())
    return {
        "total": float(raw.get("total", 0.0)),
        "measured": int(raw.get("measured", 0)),
        "files": {name: float(pct) for name, pct in raw.get("files", {}).items()},
    }


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Write the baseline with stable key ordering so diffs stay readable.

    Args:
        path: Destination path.
        baseline: The baseline to serialise.
    """
    payload = {
        "total": round(baseline["total"], 2),
        "measured": baseline["measured"],
        "files": {
            name: round(baseline["files"][name], 2)
            for name in sorted(baseline["files"])
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def find_regressions(report: Report, baseline: Baseline) -> list[Regression]:
    """Identify files whose coverage dropped below their recorded floor.

    Files absent from the baseline are new and cannot regress; newly written
    lines are governed by the diff coverage gate instead.

    Args:
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        Regressions, worst drop first.
    """
    regressions = [
        Regression(path=name, baseline=floor, current=report.files[name])
        for name, floor in baseline["files"].items()
        # A missing file was deleted or renamed; it drops out of the baseline.
        if name in report.files and report.files[name] < floor - EPSILON
    ]
    return sorted(regressions, key=lambda item: item.drop, reverse=True)


def total_regressed(report: Report, baseline: Baseline) -> bool:
    """Decide whether the overall percentage counts as a regression.

    The total is only comparable when the codebase has not shrunk. Deleting
    well-covered code lowers the project average without anything getting
    worse: a file at 100% in a project at 32% drags the mean down when it is
    removed. Gating on that would penalise ordinary refactoring and dead-code
    removal.

    Skipping the check when code was deleted hides nothing, because a genuine
    drop still shows up in the per-file gate, which callers run first, and
    newly written lines are governed by diff coverage.

    Args:
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        True if the total dropped in a way worth failing on.
    """
    if report.measured < baseline["measured"]:
        return False
    return report.total < baseline["total"] - EPSILON


def find_unrecorded(report: Report, baseline: Baseline) -> list[Unrecorded]:
    """Identify coverage the project has earned but never committed.

    The mirror image of find_regressions, and the check that makes the ratchet
    hold in a hook: a gain that is not written down is protected by nothing,
    and giving it back later trips no gate.

    Baseline entries missing from the report are deletions rather than gains,
    and are deliberately not reported - failing a push for removing a file
    would protect nothing.

    Args:
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        Unrecorded gains, biggest first.
    """
    gains = []
    for name, current in report.files.items():
        floor = baseline["files"].get(name)
        if floor is None:
            gains.append(Unrecorded(path=name, baseline=None, current=current))
        elif current > floor + EPSILON:
            gains.append(Unrecorded(path=name, baseline=floor, current=current))
    return sorted(gains, key=lambda item: item.gain, reverse=True)


def total_unrecorded(report: Report, baseline: Baseline) -> bool:
    """Decide whether the overall percentage has risen above what is recorded.

    Mirrors total_regressed, including its size guard: once the codebase has
    shrunk the two totals describe different things, and deleting poorly
    covered code raises the average without anything having been earned.

    Args:
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        True if the total gained in a way worth recording.
    """
    if report.measured < baseline["measured"]:
        return False
    return report.total > baseline["total"] + EPSILON


def raise_baseline(report: Report, baseline: Baseline) -> Baseline:
    """Build the next baseline, keeping the higher of old and new per file.

    The total and measured size are taken together from the current run rather
    than maxed independently. They describe a single snapshot, and keeping a
    stale high total against a newly smaller codebase would reintroduce the
    false positive that total_regressed exists to avoid.

    Args:
        report: The current coverage report.
        baseline: The committed baseline.

    Returns:
        The updated baseline.
    """
    files = {
        name: max(pct, baseline["files"].get(name, 0.0))
        for name, pct in report.files.items()
    }
    shrank = report.measured < baseline["measured"]
    return {
        "total": report.total if shrank else max(report.total, baseline["total"]),
        "measured": report.measured,
        "files": files,
    }
