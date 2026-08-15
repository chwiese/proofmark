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


def read_report(path: Path) -> Report:
    """Parse a coverage.py JSON report.

    Args:
        path: Path to coverage.json.

    Returns:
        The parsed report.

    Raises:
        RatchetError: If the report is missing or malformed.
    """
    if not path.exists():
        raise RatchetError(
            f"{path.name} not found - run the test suite with coverage first"
        )

    try:
        raw = json.loads(path.read_text())
        totals = raw["totals"]
        files = {
            name: float(data["summary"]["percent_covered"])
            for name, data in raw["files"].items()
        }
        # coverage.py omits the branch keys entirely when branch coverage is
        # not in effect, so a project measuring statements only is still valid.
        measured = int(totals["num_statements"]) + int(totals.get("num_branches", 0))
        return Report(
            total=float(totals["percent_covered"]), measured=measured, files=files
        )
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
