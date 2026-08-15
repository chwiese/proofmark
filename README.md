# proofmark

Test quality gates for Python projects: a per-file coverage ratchet, diff
coverage on changed lines, and mutation testing. Runs locally, at `git push`,
with no CI required.

The name comes from assaying — you put the metal through the proof, then stamp
a mark certifying it passed. Here the mark is `.coverage-baseline.json`, a
committed record of the standard the project has reached.

## What it does

`proofmark run` executes three things in order:

1. **The test suite, with coverage.** `pytest --cov` writing both a JSON and a
   Cobertura report.
2. **The per-file ratchet.** Every file has a committed coverage floor. If any
   file drops below its floor, the run fails. On success the floors are raised
   to match — never lowered.
3. **Diff coverage.** Lines changed relative to the base branch must be covered
   to a threshold (80% by default).

`proofmark run --mutants` additionally runs [mutmut](https://github.com/boxed/mutmut).
Mutation testing is **advisory and never gates** — a surviving mutant is
evidence the tests do not distinguish a behaviour, not proof of a bug. Gating on
the score encourages writing assertions that kill mutants rather than assertions
that describe behaviour.

### Why per-file, and not just a total

A single project-wide percentage hides the case that matters. Gut the tests on
a large module while adding a new, well-covered one and the total barely moves.
Per-file floors catch it; the total alone does not.

### Why the baseline is committed

If a coverage gain is never recorded, a later change can give it back
unnoticed — the "broken ratchet". Committing the baseline makes each gain
permanent, and makes any deliberate reduction show up in review as a diff.

This is also why `--check` fails when the baseline is behind what the suite
actually achieves. A gain nobody wrote down is protected by nothing: the number
pytest just printed is not a standard, the committed one is. Deleting code is
not a gain, so removals and a shrinking codebase pass.

### Deleting code does not count as a regression

Removing well-covered code lowers the project average without anything getting
worse: a file at 100% inside a project at 32% drags the mean down when it goes.
proofmark records the measured size (statements + branches) alongside the total
and skips the total gate when the codebase shrank. Nothing is hidden by this —
a genuine drop still trips the per-file gate, which runs first.

## Requirements

proofmark installs with no dependencies of its own, but it does not bundle a
toolchain either: it drives `pytest`, `diff-cover` and `mutmut` through
`uv run`, so they are resolved from the dev dependencies of the project being
checked. Set that up once per project:

```bash
uv add --dev pytest pytest-cov diff-cover   # add mutmut for --mutants
```

That split is deliberate rather than incidental:

- **`pytest` and `pytest-cov` could not work any other way.** They import the
  project's code, conftest and fixtures, so they have to come from the
  project's environment. Any project worth gating already has them.
- **`diff-cover` only reads `coverage.xml` and `git diff`,** so it *could* ship
  as a proofmark dependency. It is left to the project so that it appears in
  the project's lockfile — pinned reproducibly, and visible to `uv audit`.
  It is the one genuinely extra package proofmark asks for.
- **`mutmut` re-runs the project's test suite,** so it too must come from the
  project. It is only needed for `--mutants`.

Nothing is silently skipped if a tool is missing. A missing `diff-cover` fails
the run with the command needed to fix it, and a missing `mutmut` warns rather
than letting `--mutants` quietly do nothing.

Keeping proofmark's own install empty is what makes the pre-commit/prek hook
environment a single small wheel that builds in well under a second.

## Use as a pre-push hook

Add to `.pre-commit-config.yaml`, using whichever remote protocol you normally
clone with.

Over HTTPS:

```yaml
repos:
  - repo: https://github.com/chwiese/proofmark
    rev: v0.1.1
    hooks:
      - id: proofmark
        stages: [pre-push]
```

Over SSH:

```yaml
repos:
  - repo: git@github.com:chwiese/proofmark.git
    rev: v0.1.1
    hooks:
      - id: proofmark
        stages: [pre-push]
```

The two are equivalent, with one caveat if the repository is private: pre-commit
and prek clone hook repositories non-interactively, so HTTPS needs a git
credential helper already configured (`gh auth setup-git` sets one up) or the
clone fails with `could not read Username for 'https://github.com'`. SSH works
as long as your key is set up.

Then install the stage — `pre-commit install` alone does not cover it:

```bash
prek install --hook-type pre-push     # or: pre-commit install --hook-type pre-push
```

Commits stay fast; the suite and both gates run on `git push`.

The hook runs `--check`, which never writes the baseline — a pre-push hook
cannot add a file to the commit you are already pushing. Instead it fails if
the baseline is out of date. Without that it would report success while the
recorded standard quietly stayed at whatever it was first seeded with, gating
nothing.

Recovering costs one command and no second test run: the failing hook has
already written `coverage.json`, so `proofmark check --update` records exactly
the numbers it just showed you. Commit the baseline and push again.

## Use directly

```bash
proofmark run             # run tests, apply gates, raise the baseline
proofmark run --check     # verify without writing (what the hook runs)
proofmark run --mutants   # also run mutation testing
proofmark check           # gates only, against an existing coverage.json
proofmark check --update  # gates only, raising the baseline
```

Run `proofmark run` before committing: it raises the baseline, so a gain lands
in the same commit as the tests that earned it, and the hook then has nothing
to complain about at push time.

## Configuration

Everything is optional. Put overrides in the project's `pyproject.toml`:

```toml
[tool.proofmark]
source = "mypackage"            # pytest --cov target
diff_threshold = 80             # percent of changed lines that must be covered
compare_branch = "origin/main"  # what "changed" is measured against
exclude = ["tests", "mutants"]  # directories the ratchet ignores
```

`source` is inferred when omitted: the project name from `[project]`, with
dashes converted to underscores, if `<name>/` or `src/<name>/` exists —
otherwise `.`, which suits projects that are flat modules at the repository
root.

`exclude` is inferred too, but only for that flat case. Measuring `.` sweeps
the test suite in alongside the code, and a committed floor on a test file
records nothing anyone would act on. proofmark takes `testpaths` from
`[tool.pytest.ini_options]`, or failing that whichever of `tests/` and `test/`
exists. Setting `exclude` yourself replaces that inference rather than adding
to it. A project whose coverage is scoped to a package excludes nothing by
default, because it never measured its tests to begin with.

If the compare branch does not exist locally, the diff coverage gate is skipped
with a warning rather than failing.

## First run

`proofmark run` writes `.coverage-baseline.json` from whatever coverage
currently exists. Commit it. From then on the number can only go up — and until
you do, `--check` fails rather than passing a project nothing is gating.
