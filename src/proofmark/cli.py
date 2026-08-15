"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from proofmark import runner
from proofmark.config import Config, ConfigError, load
from proofmark.ratchet import RatchetError

# pytest's exit status when it collected nothing at all. For a ratchet, an
# empty suite is a legitimate starting state rather than a failure: seeding a
# baseline at zero is exactly how a project adopts the gate before it has
# written its first test.
PYTEST_NO_TESTS_COLLECTED = 5


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="proofmark",
        description=(
            "Test quality gates: per-file coverage ratchet, diff coverage, "
            "and mutation testing."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser(
        "run",
        help="run the test suite with coverage, then apply the gates",
    )
    run.add_argument(
        "--check",
        action="store_true",
        help="verify the gates without updating the baseline (use in hooks)",
    )
    run.add_argument(
        "--mutants",
        action="store_true",
        help="additionally run mutation testing (advisory, never gates)",
    )

    check = subcommands.add_parser(
        "check",
        help="apply the coverage gates to an existing coverage report",
    )
    check.add_argument(
        "--update",
        action="store_true",
        help="raise the baseline where coverage improved (never lowers it)",
    )

    return parser


def _run(config: Config, *, check_only: bool, mutants: bool) -> int:
    """Run the suite and apply every gate in order.

    Args:
        config: The resolved project configuration.
        check_only: Verify without writing the baseline.
        mutants: Whether to run mutation testing afterwards.

    Returns:
        A process exit status.
    """
    runner.status("Running test suite with coverage...")
    code = runner.run_pytest(config)
    if code == PYTEST_NO_TESTS_COLLECTED:
        runner.warn(
            "No tests collected - seeding the baseline from an empty suite.\n"
            "    Coverage can only go up from here."
        )
    elif code != 0:
        runner.fail("Tests failed")
        return code

    print()
    if check_only:
        runner.status("Checking per-file coverage ratchet...")
    else:
        runner.status("Checking per-file coverage ratchet (updating baseline)...")
    if (code := runner.check_ratchet(config, update=not check_only)) != 0:
        return code

    print()
    if (code := runner.check_diff_coverage(config)) != 0:
        return code

    if mutants:
        print()
        runner.run_mutation_testing(config)

    print()
    runner.status("All checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run proofmark.

    Args:
        argv: Argument list, defaulting to sys.argv.

    Returns:
        A process exit status.
    """
    args = _build_parser().parse_args(argv)

    try:
        config = load()
        if args.command == "run":
            return _run(config, check_only=args.check, mutants=args.mutants)
        return runner.check_ratchet(config, update=args.update)
    except (ConfigError, RatchetError) as exc:
        runner.fail(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
