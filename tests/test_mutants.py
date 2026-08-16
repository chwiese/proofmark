"""Tests for selecting mutants that belong to the code being committed.

The selection is a join between a unified diff and the project's own source,
so these tests cover the two parsers and the mapping between them. Subprocess
execution is stubbed - the point is which mutants get chosen, not git's or
mutmut's behaviour.
"""

import os
from pathlib import Path

from proofmark import mutants

# Reading added lines out of a unified diff


def test_added_lines_are_read_from_the_hunk_header() -> None:
    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -10,0 +11,2 @@ def thing():
+    first
+    second
"""

    assert mutants.added_lines(diff) == {"pkg/mod.py": {11, 12}}


def test_a_hunk_that_only_deletes_contributes_nothing() -> None:
    """A file that only lost lines has nothing left to mutate."""
    diff = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -10,2 +9,0 @@ def thing():
-    gone
-    also gone
"""

    assert mutants.added_lines(diff) == {}


# Telling test modules apart from the code they test


def test_pytests_naming_conventions_are_recognised() -> None:
    changed = ["tests/test_cli.py", "pkg/parser_test.py", "src/pkg/cli.py"]

    assert mutants.test_files(changed) == ["tests/test_cli.py", "pkg/parser_test.py"]


def test_a_module_merely_ending_in_test_is_not_one() -> None:
    """Pytest collects `test_*.py` and `*_test.py`, not everything alike."""
    assert mutants.test_files(["src/pkg/contest.py", "tests/conftest.py"]) == []


# Mapping changed lines onto the functions that own them

SOURCE = '''"""A module."""

CONSTANT = 1


def top_level(value):
    return value + 1


class Holder:
    def method(self):
        return 2
'''


def test_a_changed_line_selects_the_function_containing_it() -> None:
    assert mutants.functions_at(SOURCE, {7}) == [mutants.Function("top_level")]


def test_a_method_is_selected_with_its_class() -> None:
    assert mutants.functions_at(SOURCE, {12}) == [mutants.Function("method", "Holder")]


DECORATED = """import functools


@functools.cache
def memoised(value):
    return value + 1


@dataclass
class Frozen:
    def method(self):
        return 2


class Plain:
    @staticmethod
    def helper(value):
        return value + 3

    @property
    def size(self):
        return 4
"""


def test_a_decorated_function_is_not_selected() -> None:
    """Mutmut refuses to mutate these, so selecting one asks for nothing."""
    assert mutants.functions_at(DECORATED, {5}) == []


def test_a_method_of_a_decorated_class_is_not_selected() -> None:
    assert mutants.functions_at(DECORATED, {11}) == []


def test_a_static_method_is_selected() -> None:
    """The one decorator mutmut still mutates through."""
    assert mutants.functions_at(DECORATED, {17}) == [
        mutants.Function("helper", "Plain")
    ]


def test_a_property_is_not_selected() -> None:
    assert mutants.functions_at(DECORATED, {21}) == []


# Naming the mutants a function owns


def test_a_glob_names_every_mutant_of_a_function() -> None:
    """Mutmut selects mutants by fnmatch against its own generated names."""
    glob = mutants.mutant_glob("src/proofmark/runner.py", mutants.Function("status"))

    assert glob == "proofmark.runner.x_status__mutmut_*"


def test_a_method_glob_carries_the_class_name() -> None:
    glob = mutants.mutant_glob("src/pkg/mod.py", mutants.Function("method", "Holder"))

    assert glob == "pkg.mod.xǁHolderǁmethod__mutmut_*"


def test_a_package_init_collapses_into_the_package() -> None:
    """Mutmut names a mutant after the importable module, not the file."""
    glob = mutants.mutant_glob("src/pkg/__init__.py", mutants.Function("thing"))

    assert glob == "pkg.x_thing__mutmut_*"


def test_a_method_is_reported_under_its_class() -> None:
    target = mutants.Target("mod.py", mutants.Function("method", "Holder"))

    assert target.display == "Holder.method"


# Reading mutmut's recorded verdicts


def test_verdicts_are_read_per_mutant() -> None:
    output = """    pkg.mod.x_thing__mutmut_1: killed
    pkg.mod.x_thing__mutmut_2: survived
"""

    assert mutants.parse_verdicts(output) == {
        "pkg.mod.x_thing__mutmut_1": "killed",
        "pkg.mod.x_thing__mutmut_2": "survived",
    }


def test_lines_that_are_not_verdicts_are_ignored() -> None:
    """Mutmut prints headings and progress around the list."""
    output = "Mutant results\n--------------\n    pkg.mod.x_a__mutmut_1: killed\n"

    assert mutants.parse_verdicts(output) == {"pkg.mod.x_a__mutmut_1": "killed"}


def test_progress_frames_are_left_out_of_reported_output() -> None:
    """Captured, mutmut's spinner becomes hundreds of lines of braille."""
    output = (
        "⠋ Generating mutants\n"
        "⠙ Generating mutants\n"
        "    done in 5ms\n"
        "⠹ Running clean tests\n"
        "FAILED tests/test_thing.py::test_it\n"
    )

    assert mutants.without_progress(output) == (
        "    done in 5ms\nFAILED tests/test_thing.py::test_it"
    )


# Selecting targets from a working tree


def test_selection_pairs_a_function_with_its_file(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(SOURCE)

    assert mutants.select(tmp_path, {"mod.py": {7}}) == [
        mutants.Target("mod.py", mutants.Function("top_level"))
    ]


def test_a_file_that_is_not_python_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# changed\n")

    assert mutants.select(tmp_path, {"notes.md": {1}}) == []


def test_a_test_module_is_not_a_mutation_target(tmp_path: Path) -> None:
    """Mutmut mutates the code under test, not the tests.

    Asking it about one would be asking for a pattern matching nothing.
    """
    (tmp_path / "test_thing.py").write_text(SOURCE)

    assert mutants.select(tmp_path, {"test_thing.py": {7}}) == []


def test_a_deleted_file_is_ignored(tmp_path: Path) -> None:
    """The diff still names a file the commit removed."""
    assert mutants.select(tmp_path, {"gone.py": {1}}) == []


def test_a_file_that_will_not_parse_is_skipped(tmp_path: Path) -> None:
    """A project may use syntax newer than the interpreter proofmark runs on."""
    (tmp_path / "future.py").write_text("def f(:\n")

    assert mutants.select(tmp_path, {"future.py": {1}}) == []


# Deciding what still needs running

TARGET = mutants.Target("mod.py", mutants.Function("thing"))
DECIDED = {
    "mod.x_thing__mutmut_1": "killed",
    "mod.x_thing__mutmut_2": "survived",
}


def generated(tmp_path: Path, *, stale: bool) -> None:
    """Lay out a source file and the mutants copy mutmut made of it."""
    source = tmp_path / "mod.py"
    source.write_text("def thing():\n    return 1\n")
    copy = tmp_path / "mutants" / "mod.py"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("generated\n")
    # mutmut compares these two mtimes to decide the source has moved on.
    os.utime(source, (0, 200))
    os.utime(copy, (0, 100 if stale else 300))


def test_a_settled_verdict_is_not_run_again(tmp_path: Path) -> None:
    generated(tmp_path, stale=False)

    to_run, known = mutants.partition(tmp_path, [TARGET], DECIDED)

    assert (to_run, known) == ([], [TARGET])


def test_an_edited_file_is_run_again(tmp_path: Path) -> None:
    """Mutmut only invalidates an edited function during its own next run."""
    generated(tmp_path, stale=True)

    to_run, known = mutants.partition(tmp_path, [TARGET], DECIDED)

    assert (to_run, known) == ([TARGET], [])


def test_an_undecided_mutant_is_run(tmp_path: Path) -> None:
    generated(tmp_path, stale=False)
    verdicts = DECIDED | {"mod.x_thing__mutmut_3": "not checked"}

    to_run, known = mutants.partition(tmp_path, [TARGET], verdicts)

    assert (to_run, known) == ([TARGET], [])


def test_a_cold_cache_runs_everything(tmp_path: Path) -> None:
    """With nothing recorded, there is no verdict to trust or to skip."""
    generated(tmp_path, stale=False)

    to_run, known = mutants.partition(tmp_path, [TARGET], {})

    assert (to_run, known) == ([TARGET], [])


def test_a_function_with_no_mutants_is_dropped(tmp_path: Path) -> None:
    """Mutmut treats a pattern matching nothing as an error, so never ask."""
    generated(tmp_path, stale=False)

    to_run, known = mutants.partition(
        tmp_path, [TARGET], {"mod.x_other__mutmut_1": "killed"}
    )

    assert (to_run, known) == ([], [])


def test_a_function_mutmut_has_never_seen_is_run(tmp_path: Path) -> None:
    """A new file is absent from the verdicts because it is new, not empty.

    Dropping it would blind the pass to exactly the code it exists to check.
    """
    (tmp_path / "mod.py").write_text("def thing():\n    return 1\n")

    to_run, known = mutants.partition(
        tmp_path, [TARGET], {"other.x_thing__mutmut_1": "killed"}
    )

    assert (to_run, known) == ([TARGET], [])


# Counting what got through


def test_survivors_are_counted_against_the_mutants_of_a_function() -> None:
    verdicts = {
        "mod.x_thing__mutmut_1": "killed",
        "mod.x_thing__mutmut_2": "survived",
        "mod.x_thing__mutmut_3": "survived",
    }

    assert mutants.summarise([TARGET], verdicts) == [
        mutants.Summary("mod.py", "thing", survived=2, total=3)
    ]


def test_a_function_whose_mutants_all_died_is_not_reported() -> None:
    verdicts = {"mod.x_thing__mutmut_1": "killed"}

    assert mutants.summarise([TARGET], verdicts) == []


def test_the_worst_function_is_reported_first() -> None:
    other = mutants.Target("mod.py", mutants.Function("other"))
    verdicts = {
        "mod.x_thing__mutmut_1": "survived",
        "mod.x_other__mutmut_1": "survived",
        "mod.x_other__mutmut_2": "survived",
    }

    assert [item.function for item in mutants.summarise([TARGET, other], verdicts)] == [
        "other",
        "thing",
    ]
