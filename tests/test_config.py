"""Tests for project discovery and configuration.

Source inference matters most here: real projects range from a package
directory alongside an entry point module, through src layouts, to a handful
of flat modules at the repository root. None of them should need to configure
anything.
"""

import textwrap
from pathlib import Path

import pytest

from proofmark.config import (
    DEFAULT_COMPARE_BRANCH,
    DEFAULT_DIFF_THRESHOLD,
    ConfigError,
    find_project_root,
    load,
)


def write_pyproject(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


# Project discovery


def test_finds_root_in_current_directory(tmp_path: Path) -> None:
    write_pyproject(tmp_path, '[project]\nname = "thing"\n')

    assert find_project_root(tmp_path) == tmp_path.resolve()


def test_finds_root_from_a_subdirectory(tmp_path: Path) -> None:
    write_pyproject(tmp_path, '[project]\nname = "thing"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_project_root(nested) == tmp_path.resolve()


def test_missing_pyproject_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no pyproject.toml"):
        find_project_root(tmp_path / "empty")


# Source inference


def test_infers_package_directory_named_after_project(tmp_path: Path) -> None:
    """The common layout: a package directory beside an entry point module."""
    write_pyproject(tmp_path, '[project]\nname = "myapp"\n')
    (tmp_path / "myapp").mkdir()

    assert load(tmp_path).source == "myapp"


def test_infers_src_layout(tmp_path: Path) -> None:
    write_pyproject(tmp_path, '[project]\nname = "thing"\n')
    (tmp_path / "src" / "thing").mkdir(parents=True)

    assert load(tmp_path).source == str(Path("src") / "thing")


def test_normalises_dashes_to_underscores(tmp_path: Path) -> None:
    """PyPI names use dashes; the importable package uses underscores."""
    write_pyproject(tmp_path, '[project]\nname = "my-data-tool"\n')
    (tmp_path / "my_data_tool").mkdir()

    assert load(tmp_path).source == "my_data_tool"


def test_falls_back_to_dot_for_flat_layouts(tmp_path: Path) -> None:
    """The flat layout: loose modules at the repository root, no package dir."""
    write_pyproject(tmp_path, '[project]\nname = "flatproj"\n')
    (tmp_path / "flatproj.py").write_text("")

    assert load(tmp_path).source == "."


def test_falls_back_to_dot_without_a_project_name(tmp_path: Path) -> None:
    write_pyproject(tmp_path, "[tool.something]\nkey = 1\n")

    assert load(tmp_path).source == "."


def test_explicit_source_wins_over_inference(tmp_path: Path) -> None:
    write_pyproject(
        tmp_path,
        """
        [project]
        name = "thing"

        [tool.proofmark]
        source = "custom_dir"
        """,
    )
    (tmp_path / "thing").mkdir()

    assert load(tmp_path).source == "custom_dir"


# Defaults and overrides


def test_defaults_apply_without_a_config_section(tmp_path: Path) -> None:
    write_pyproject(tmp_path, '[project]\nname = "thing"\n')

    config = load(tmp_path)

    assert config.diff_threshold == DEFAULT_DIFF_THRESHOLD
    assert config.compare_branch == DEFAULT_COMPARE_BRANCH


def test_overrides_are_read(tmp_path: Path) -> None:
    write_pyproject(
        tmp_path,
        """
        [project]
        name = "thing"

        [tool.proofmark]
        diff_threshold = 95
        compare_branch = "origin/develop"
        """,
    )

    config = load(tmp_path)

    assert config.diff_threshold == 95
    assert config.compare_branch == "origin/develop"


def test_derived_paths_sit_at_the_project_root(tmp_path: Path) -> None:
    write_pyproject(tmp_path, '[project]\nname = "thing"\n')

    config = load(tmp_path)

    assert config.baseline == tmp_path.resolve() / ".coverage-baseline.json"
    assert config.coverage_json == tmp_path.resolve() / "coverage.json"
    assert config.coverage_xml == tmp_path.resolve() / "coverage.xml"


# Validation


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[tool.proofmark]\nsource = 3\n", "source must be a string"),
        ("[tool.proofmark]\ndiff_threshold = 'high'\n", "must be an integer"),
        ("[tool.proofmark]\ndiff_threshold = 101\n", "between 0 and 100"),
        ("[tool.proofmark]\ndiff_threshold = -1\n", "between 0 and 100"),
        ("[tool.proofmark]\ncompare_branch = 5\n", "compare_branch must be a string"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    write_pyproject(tmp_path, body)

    with pytest.raises(ConfigError, match=message):
        load(tmp_path)


@pytest.mark.parametrize("threshold", [0, 100])
def test_threshold_bounds_are_inclusive(tmp_path: Path, threshold: int) -> None:
    """0 disables the diff gate and 100 demands every changed line; both valid."""
    write_pyproject(tmp_path, f"[tool.proofmark]\ndiff_threshold = {threshold}\n")

    assert load(tmp_path).diff_threshold == threshold


def test_boolean_is_not_accepted_as_a_threshold(tmp_path: Path) -> None:
    """Bool is a subclass of int in Python, so it needs an explicit guard."""
    write_pyproject(tmp_path, "[tool.proofmark]\ndiff_threshold = true\n")

    with pytest.raises(ConfigError, match="must be an integer"):
        load(tmp_path)
