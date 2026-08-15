"""Project discovery and configuration.

Configuration lives in the checked project's pyproject.toml under
[tool.proofmark]. Every key is optional; the defaults are chosen so that a
typical uv + pytest project needs no configuration at all.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_SECTION = "proofmark"
BASELINE_NAME = ".coverage-baseline.json"

DEFAULT_DIFF_THRESHOLD = 80
DEFAULT_COMPARE_BRANCH = "origin/main"


class ConfigError(Exception):
    """Raised when the project or its configuration cannot be resolved."""


@dataclass(frozen=True)
class Config:
    """Resolved settings for one project."""

    root: Path
    source: str
    diff_threshold: int
    compare_branch: str

    @property
    def baseline(self) -> Path:
        """Path to the committed coverage baseline."""
        return self.root / BASELINE_NAME

    @property
    def coverage_json(self) -> Path:
        """Path to the coverage.py JSON report, read by the per-file gate."""
        return self.root / "coverage.json"

    @property
    def coverage_xml(self) -> Path:
        """Path to the Cobertura report, read by diff-cover.

        diff-cover accepts only Cobertura, Clover or JaCoCo XML, or LCov - it
        cannot read coverage.py's JSON, which is why both reports are written.
        """
        return self.root / "coverage.xml"


def find_project_root(start: Path | None = None) -> Path:
    """Locate the project root by walking up in search of pyproject.toml.

    Args:
        start: Directory to start from. Defaults to the current directory.

    Returns:
        The directory containing pyproject.toml.

    Raises:
        ConfigError: If no pyproject.toml is found in any parent directory.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"no pyproject.toml found at or above {current}")


def _infer_source(root: Path, pyproject: dict[str, object]) -> str:
    """Guess the coverage target from the project layout.

    Prefers an importable package directory named after the project, since
    that is the common `src`-less layout. Projects that are a handful of flat
    modules at the repository root have no such directory, and measuring "."
    is the only sensible option there.

    Args:
        root: The project root.
        pyproject: Parsed pyproject.toml contents.

    Returns:
        A value suitable for passing to `pytest --cov=`.
    """
    project = pyproject.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str):
            module = name.replace("-", "_")
            for candidate in (root / module, root / "src" / module):
                if candidate.is_dir():
                    return str(candidate.relative_to(root))
    return "."


def load(start: Path | None = None) -> Config:
    """Load configuration for the project containing `start`.

    Args:
        start: Directory to resolve the project from. Defaults to cwd.

    Returns:
        The resolved configuration.

    Raises:
        ConfigError: If the project cannot be found or the config is malformed.
    """
    root = find_project_root(start)

    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    tool = pyproject.get("tool")
    section: dict[str, object] = {}
    if isinstance(tool, dict):
        candidate = tool.get(CONFIG_SECTION)
        if isinstance(candidate, dict):
            section = candidate

    source = section.get("source")
    if source is not None and not isinstance(source, str):
        raise ConfigError("tool.proofmark.source must be a string")

    threshold = section.get("diff_threshold", DEFAULT_DIFF_THRESHOLD)
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise ConfigError("tool.proofmark.diff_threshold must be an integer")
    if not 0 <= threshold <= 100:
        raise ConfigError("tool.proofmark.diff_threshold must be between 0 and 100")

    branch = section.get("compare_branch", DEFAULT_COMPARE_BRANCH)
    if not isinstance(branch, str):
        raise ConfigError("tool.proofmark.compare_branch must be a string")

    return Config(
        root=root,
        source=source if source is not None else _infer_source(root, pyproject),
        diff_threshold=threshold,
        compare_branch=branch,
    )
