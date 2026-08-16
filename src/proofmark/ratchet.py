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


@dataclass(frozen=True)
class Counts:
    """How much of one file is measurable, and how much of it is not covered.

    Recorded as counts rather than a percentage because a percentage is a
    ratio: deleting lines moves it without anything having got better or
    worse. Only the uncovered count distinguishes a deletion from an
    uncovering, and that distinction is what keeps the gate from penalising
    dead-code removal.
    """

    missing: int
    # Statements plus branches - the denominator behind the percentage.
    measured: int

    @property
    def percent(self) -> float:
        """The percentage covered."""
        if self.measured == 0:
            # coverage.py's reading of a file with nothing to cover.
            return 100.0
        return 100.0 * (self.measured - self.missing) / self.measured


class Baseline(TypedDict):
    """Committed coverage floors for one project."""

    total: float
    measured: int
    files: dict[str, Counts]


@dataclass(frozen=True)
class Report:
    """A parsed coverage.py JSON report."""

    total: float
    # Statements plus branches - the denominator behind percent_covered.
    # Tracked so the total floor can be skipped when the codebase shrinks.
    measured: int
    files: dict[str, Counts]


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


def _counts(summary: Mapping[str, float]) -> Counts:
    """Read one file's counts out of its coverage.py summary block.

    Statements and branches are added together, which is the denominator
    coverage.py itself reports percentages against.

    Args:
        summary: The `summary` block for one file.

    Returns:
        The file's uncovered and measurable counts.
    """
    # The branch keys are absent unless branch coverage is in effect.
    covered = int(summary["covered_lines"]) + int(summary.get("covered_branches", 0))
    measured = int(summary["num_statements"]) + int(summary.get("num_branches", 0))
    return Counts(missing=measured - covered, measured=measured)


def _summarise(counts: Iterable[Counts]) -> tuple[float, int]:
    """Recompute a total and measured size from per-file counts.

    Summing the numerators and denominators reproduces exactly what coverage.py
    would have reported for this subset of files.

    Args:
        counts: The counts of each file being included.

    Returns:
        The percentage covered and the measured size.
    """
    items = list(counts)  # iterated twice below
    total = Counts(
        missing=sum(item.missing for item in items),
        measured=sum(item.measured for item in items),
    )
    return total.percent, total.measured


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
        files = {
            name: _counts(data["summary"])
            for name, data in raw["files"].items()
            if not _is_excluded(name, prefixes)
        }
        if len(files) == len(raw["files"]):
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
            total, measured = _summarise(files.values())
        return Report(total=total, measured=measured, files=files)
    except (KeyError, TypeError, ValueError) as exc:
        raise RatchetError(f"could not parse {path.name}: {exc}") from exc


def _read_floor(name: str, entry: object) -> Counts:
    """Parse one recorded floor.

    Args:
        name: The file the entry belongs to, for the error message.
        entry: The value recorded against it.

    Returns:
        The floor.

    Raises:
        ValueError: If the entry is not a `[missing, measured]` pair.
    """
    if isinstance(entry, list) and len(entry) == 2:
        return Counts(missing=int(entry[0]), measured=int(entry[1]))
    raise ValueError(f"expected [missing, measured] for {name!r}, found {entry!r}")


def read_baseline(path: Path) -> Baseline:
    """Load the committed baseline, treating a missing file as an empty one.

    Args:
        path: Path to the baseline JSON file.

    Returns:
        The baseline mapping.

    Raises:
        RatchetError: If the file exists but cannot be read.
    """
    if not path.exists():
        return {"total": 0.0, "measured": 0, "files": {}}

    try:
        raw = json.loads(path.read_text())
        return {
            "total": float(raw.get("total", 0.0)),
            "measured": int(raw.get("measured", 0)),
            "files": {
                name: _read_floor(name, entry)
                for name, entry in raw.get("files", {}).items()
            },
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RatchetError(
            f"could not parse {path.name}: {exc}\n"
            f"    Delete it and run proofmark run to record it afresh."
        ) from exc


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Write the baseline with stable key ordering so diffs stay readable.

    Rendered by hand rather than with `json.dumps(indent=2)`, which would put
    each count on its own line and turn a one-line-per-file record into four.
    The baseline is committed, so how it diffs is part of its job.

    Args:
        path: Destination path.
        baseline: The baseline to serialise.
    """
    entries = ",\n".join(
        f"    {json.dumps(name)}: "
        f"[{baseline['files'][name].missing}, {baseline['files'][name].measured}]"
        for name in sorted(baseline["files"])
    )
    files = f"{{\n{entries}\n  }}" if entries else "{}"
    path.write_text(
        "{\n"
        f'  "total": {json.dumps(round(baseline["total"], 2))},\n'
        f'  "measured": {json.dumps(baseline["measured"])},\n'
        f'  "files": {files}\n'
        "}\n"
    )


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
        Regression(
            path=name, baseline=floor.percent, current=report.files[name].percent
        )
        for name, floor in baseline["files"].items()
        # A missing file was deleted or renamed; it drops out of the baseline.
        if name in report.files and _file_regressed(report.files[name], floor)
    ]
    return sorted(regressions, key=lambda item: item.drop, reverse=True)


def _file_regressed(current: Counts, floor: Counts) -> bool:
    """Decide whether one file got worse, rather than merely smaller.

    A percentage is a ratio, so deleting covered lines lowers it without
    anything having stopped being tested. Comparing percentages alone would
    fail a push for removing dead code - the very thing total_regressed is
    written to avoid, one level down.

    When the file has shrunk the question is asked in counts instead: did any
    line that was covered stop being covered? That catches the case a bare
    size guard would miss, where deleting the only caller of a helper both
    shrinks the file and leaves the helper untested.

    Args:
        current: The file's counts in this run.
        floor: Its recorded standard.

    Returns:
        True if the file regressed.
    """
    if current.measured < floor.measured:
        return current.missing > floor.missing
    return current.percent < floor.percent - EPSILON


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

    Deletions are not gains, in either of the two shapes they take. A baseline
    entry missing from the report is a removed file. A file that shrank has a
    higher ratio only because its denominator fell - nothing was earned by it,
    and failing a push for removing dead code would be the same false positive
    the regression gate refuses to raise.

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
            gains.append(Unrecorded(path=name, baseline=None, current=current.percent))
        elif current.measured < floor.measured:
            continue
        elif current.percent > floor.percent + EPSILON:
            gains.append(
                Unrecorded(path=name, baseline=floor.percent, current=current.percent)
            )
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


def _raise_floor(current: Counts, floor: Counts | None) -> Counts:
    """Build one file's next floor, never lowering the standard it records.

    A shrunk file adopts the current counts outright, for the same reason the
    total does: they are a matched snapshot, and holding counts measured
    against a size the file no longer has would leave it looking shrunk on
    every later run and never recording anything again.

    Args:
        current: The file's counts in this run.
        floor: Its recorded standard, or None if it is not tracked yet.

    Returns:
        The floor to record.
    """
    if floor is None:
        return current
    if current.measured < floor.measured:
        return current
    if current.percent >= floor.percent:
        return current
    # Hold the higher standard: callers only reach here once the gate has
    # passed, so this is the sub-EPSILON sliver that would otherwise be
    # written in and become the new normal.
    return floor


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
        name: _raise_floor(current, baseline["files"].get(name))
        for name, current in report.files.items()
    }
    shrank = report.measured < baseline["measured"]
    return {
        "total": report.total if shrank else max(report.total, baseline["total"]),
        "measured": report.measured,
        "files": files,
    }
