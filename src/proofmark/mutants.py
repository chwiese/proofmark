"""Selecting the mutants that belong to the code being committed.

mutmut works one whole source tree at a time, which is too much to ask of a
commit. This module narrows it to the functions the staged diff touched, by
joining a unified diff against the project's own source.

Deliberately independent of coverage.json: producing one means running the
whole suite under coverage instrumentation, which is the cost that would put
mutation testing out of reach at commit time. Whether a line is covered is
mutmut's own question anyway, answered from its stats cache under
`mutate_only_covered_lines`.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeIs

# The `+c,d` half of a hunk header. The count is omitted for a single line.
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")

# The path on the `+++` line, minus git's `b/` prefix.
TARGET_FILE = re.compile(r"^\+\+\+ b/(.*)$")

# What mutmut puts either side of a class name when mangling a method.
CLASS_FENCE = "ǁ"

# pytest's default idea of a test module, from its `python_files` setting.
TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")

# One line of `mutmut results`, which indents each mutant under a heading.
VERDICT = re.compile(r"^\s+(\S+): (\w[\w ]*)$")

# A frame of mutmut's spinner, which is drawn with braille block characters.
PROGRESS_FRAME = re.compile(r"^[⠀-⣿]")

# The verdict meaning the tests did not notice the mutation.
SURVIVED = "survived"

# What mutmut records against a mutant it has not run yet.
UNTESTED = "not checked"


@dataclass(frozen=True)
class Function:
    """One function mutmut can mutate: a module-level def, or a method."""

    name: str
    # None for a module-level function. mutmut's mangling carries a single
    # class name, so a method of a nested class has no name it could produce.
    class_name: str | None = None


@dataclass(frozen=True)
class Target:
    """One function the commit touched, and the mutants that belong to it."""

    path: str
    function: Function

    @property
    def glob(self) -> str:
        """The pattern matching every mutant of this function."""
        return mutant_glob(self.path, self.function)

    @property
    def display(self) -> str:
        """How the function is named in the report."""
        if self.function.class_name is None:
            return self.function.name
        return f"{self.function.class_name}.{self.function.name}"


@dataclass(frozen=True)
class Summary:
    """How one function's mutants fared."""

    path: str
    function: str
    survived: int
    total: int


def added_lines(diff: str) -> dict[str, set[int]]:
    """Read the line numbers a unified diff adds, per file.

    Only added lines matter: a deletion leaves nothing behind to mutate.

    Args:
        diff: The output of `git diff --unified=0`.

    Returns:
        Added line numbers, keyed by the path they belong to.
    """
    result: dict[str, set[int]] = {}
    path = ""
    for line in diff.splitlines():
        if match := TARGET_FILE.match(line):
            path = match.group(1)
        elif (match := HUNK.match(line)) and path:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count:
                result.setdefault(path, set()).update(range(start, start + count))
    return result


def functions_at(source: str, lines: Collection[int]) -> list[Function]:
    """Find the functions that own the given lines of a module.

    Args:
        source: The module's source.
        lines: Line numbers, 1-based.

    Returns:
        The functions owning at least one of the lines, in source order.
    """
    found: list[Function] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            # A decorated class is skipped whole, so none of its methods have
            # mutants to ask for. Nor does a class nested inside this one:
            # mutmut's mangling carries a single class name.
            if node.decorator_list:
                continue
            found.extend(
                Function(child.name, node.name)
                for child in node.body
                if _is_mutatable(child) and _touches(child, lines)
            )
        elif _is_mutatable(node) and _touches(node, lines):
            found.append(Function(node.name))
    return found


def _is_mutatable(node: ast.stmt) -> TypeIs[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Whether mutmut generates mutants for a definition at all.

    Mirrors mutmut's own rule. It leaves decorated functions alone because
    copying them for its trampoline can re-run the decorator's side effects,
    and because a @property breaks the trampoline outright. Only @staticmethod
    and @classmethod are predictable enough to mutate through.

    Selecting a function mutmut skips would ask `mutmut run` for a pattern
    matching nothing, which it treats as an error.

    Args:
        node: A statement from a module or class body.

    Returns:
        True if the node is a function mutmut would mutate.
    """
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    if not node.decorator_list:
        return True
    return len(node.decorator_list) == 1 and (
        isinstance(only := node.decorator_list[0], ast.Name)
        and only.id in ("staticmethod", "classmethod")
    )


def mutant_glob(path: str, function: Function) -> str:
    """Build the fnmatch pattern matching every mutant of one function.

    Reproduces the name mutmut generates for a mutant, which is the module
    path, the mangled function name, and a serial number. Mangling is mutmut's
    own scheme: `x_` for a plain function, and the class name fenced by U+01C1
    for a method.

    Args:
        path: The source file, relative to the project root.
        function: The function within it.

    Returns:
        A pattern to pass to `mutmut run`.
    """
    module = path.removesuffix(".py").replace("\\", "/").replace("/", ".")
    module = module.removeprefix("src.").replace(".__init__", "")
    if function.class_name is None:
        mangled = f"x_{function.name}"
    else:
        mangled = f"x{CLASS_FENCE}{function.class_name}{CLASS_FENCE}{function.name}"
    return f"{module}.{mangled}__mutmut_*"


def test_files(changed: Iterable[str]) -> list[str]:
    """Pick out the paths pytest would collect as test modules.

    A test module is not code under test: mutmut does not mutate it, so the
    mutation pass would never notice you had broken one. Running the ones you
    staged is what closes that gap.

    Args:
        changed: The paths the commit touches.

    Returns:
        Those that are test modules, in the order given.
    """
    return [path for path in changed if TEST_FILE.search(path.replace("\\", "/"))]


def without_progress(output: str) -> str:
    """Drop mutmut's progress frames from output being reported.

    Its spinner redraws itself with a carriage return, which is fine on a
    terminal and becomes hundreds of lines of braille once captured. When the
    thing being reported is a test failure, that is what buries it.

    Args:
        output: Captured output from mutmut.

    Returns:
        The same output with the progress frames removed.
    """
    return "\n".join(
        line for line in output.splitlines() if not PROGRESS_FRAME.match(line)
    )


def select(root: Path, changed: Mapping[str, Collection[int]]) -> list[Target]:
    """Find the functions a set of changed lines belongs to.

    Args:
        root: The project root the paths are relative to.
        changed: Line numbers that changed, keyed by path.

    Returns:
        One target per function, in path then source order.
    """
    targets: list[Target] = []
    for path in sorted(changed):
        if not path.endswith(".py") or TEST_FILE.search(path.replace("\\", "/")):
            continue
        source = root / path
        try:
            text = source.read_text()
        except OSError:
            # The diff names files the commit deleted, and paths outside the
            # project root when it sits below the repository root.
            continue
        try:
            functions = functions_at(text, changed[path])
        except SyntaxError:
            # A project can be written for a newer Python than proofmark runs
            # on. Mutation testing is advisory, so this is not worth failing.
            continue
        targets.extend(Target(path, function) for function in functions)
    return targets


def summarise(targets: Sequence[Target], verdicts: Mapping[str, str]) -> list[Summary]:
    """Count the mutants that got through, per function.

    Args:
        targets: The functions the commit touched.
        verdicts: Verdicts by mutant name.

    Returns:
        One row per function that has a survivor, worst first.
    """
    rows = []
    for target in targets:
        recorded = [
            verdict
            for name, verdict in verdicts.items()
            if fnmatch.fnmatch(name, target.glob)
        ]
        survived = recorded.count(SURVIVED)
        if survived:
            rows.append(
                Summary(
                    path=target.path,
                    function=target.display,
                    survived=survived,
                    total=len(recorded),
                )
            )
    return sorted(rows, key=lambda row: row.survived, reverse=True)


def partition(
    root: Path, targets: Sequence[Target], verdicts: Mapping[str, str]
) -> tuple[list[Target], list[Target]]:
    """Split the targets into those still to test and those already answered.

    Naming mutants explicitly defeats mutmut's own cache - it re-runs anything
    it is asked for by name - so the skipping has to happen here. Without it
    the cost of a commit would grow with the branch behind it.

    A verdict is only trusted while the source it was measured against is
    untouched, because mutmut clears the verdicts of an edited function during
    its next generation pass, not before.

    Args:
        root: The project root.
        targets: The functions the commit touched.
        verdicts: Verdicts recorded by the last run, by mutant name.

    Returns:
        The targets to test, and the targets whose verdicts still stand.
    """
    if not verdicts:
        # Nothing recorded, so nothing to trust and nothing to skip.
        return list(targets), []

    to_run: list[Target] = []
    known: list[Target] = []
    for target in targets:
        recorded = [
            verdict
            for name, verdict in verdicts.items()
            if fnmatch.fnmatch(name, target.glob)
        ]
        current = _is_generated_from_current(root, target.path)
        if not recorded:
            # Absent from the verdicts for one of two reasons. If mutmut has
            # generated from this file as it stands, the function genuinely has
            # no mutants, and mutmut refuses a pattern matching nothing. If it
            # has not, the function is simply new - which is the case this pass
            # exists for, so it has to be tested rather than assumed empty.
            if not current:
                to_run.append(target)
            continue
        if UNTESTED in recorded or not current:
            to_run.append(target)
        else:
            known.append(target)
    return to_run, known


def _is_generated_from_current(root: Path, path: str) -> bool:
    """Whether mutmut's copy of a file was made from the file as it stands now.

    The same comparison mutmut makes when deciding a source file is unmodified.

    Args:
        root: The project root.
        path: A source path relative to it.

    Returns:
        True if the copy is no older than the source.
    """
    try:
        return (root / "mutants" / path).stat().st_mtime >= (
            root / path
        ).stat().st_mtime
    except OSError:
        return False


def parse_verdicts(output: str) -> dict[str, str]:
    """Read the verdict recorded against each mutant.

    Args:
        output: The output of `mutmut results --all True`.

    Returns:
        A verdict per mutant name. Anything unparseable is left out, so a
        change to mutmut's output costs a redundant re-run rather than a
        wrong answer.
    """
    return {
        match.group(1): match.group(2)
        for line in output.splitlines()
        if (match := VERDICT.match(line))
    }


def _touches(
    node: ast.FunctionDef | ast.AsyncFunctionDef, lines: Collection[int]
) -> bool:
    """Whether any of the lines falls inside a definition."""
    start, end = _span(node)
    return any(start <= line <= end for line in lines)


def _span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """The line range a definition occupies, decorators included.

    A decorator is part of what the function does, so editing one is editing
    the function. ast reports the definition's own line, with the decorators
    sitting above it.

    Args:
        node: The definition.

    Returns:
        First and last line, both inclusive.
    """
    start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
    return start, node.end_lineno or node.lineno
