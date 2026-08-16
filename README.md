# proofmark

Test quality gates for Python projects: a per-file coverage ratchet, diff
coverage on changed lines, and mutation testing. Runs locally, at `git commit`
and `git push`, with no CI required.

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

`proofmark commit` is a separate, smaller command scoped to the staged diff. It
does two things:

1. **The tests reaching the code you changed.** These **gate** — a failing test
   blocks the commit. A broken test is not a judgement call.
2. **Mutation testing of the changed functions,** via
   [mutmut](https://github.com/boxed/mutmut), reporting the mutants that
   survived.

Mutation testing is **advisory and never gates** — a surviving mutant is
evidence the tests do not distinguish a behaviour, not proof of a bug. Gating on
the score encourages writing assertions that kill mutants rather than assertions
that describe behaviour.

### Why this runs at commit, not push

Because of what each check costs, not how much it matters.

The coverage gates cannot be made cheap. A per-file ratchet needs the
project's coverage, and that means the whole suite, every time, run under
tracing, with two reports written and a second tool run over them. None of it
scopes down to the commit in front of you: a file's coverage cannot be
established from a subset of the tests, and diff coverage is measured against
the base branch rather than the last commit. So the gates run once, at push,
where paying for all of it makes sense.

Mutation testing does scope down — to the functions the diff touched — and
that is what makes it affordable per commit. Which is fortunate, because it is
the check that most needs to be early. A mutant that got through says a test
you just wrote does not pin the behaviour down.

It does not gate, so the commit is made either way and the fix is a fixup or an
amend whenever you hear about it. What the stage changes is *which* commit that
is. Told now, it is the one you just wrote: still the tip, still fresh, and
`git commit --amend` away. Told at push, it is somewhere under a stack of
commits you have moved on from, at the one moment you were trying to leave —
and the alternative to going back for it is pushing anyway.

So the two stages divide the work:

| stage | what it checks | gates? |
| --- | --- | --- |
| pre-commit | tests reaching the staged diff; test modules you edited | yes |
| pre-commit | mutants of the changed functions | no |
| pre-push | the whole suite, the ratchet, diff coverage | yes |

The commit-stage tests are deliberately partial: a test that never executes
your change can still break, and only the whole suite at push time will say so.
That is the trade — fast feedback where you can act, completeness before the
code leaves your machine.

### How the tests get chosen

In two steps, owned by two tools. Which *functions* are in scope is
proofmark's: the staged diff gives the changed lines, the source gives the
function each line sits in, and the recorded verdicts say which of those still
need testing. Which *tests* belong to those functions is mutmut's.

That second step is left to it deliberately. It has to know which test reaches
which function in order to test a mutant at all, so before mutating it rebuilds
that map, picks exactly the tests for the functions it was asked about, runs
them against unmutated code, and stops if any fail. proofmark reports that
rather than repeating it, because the map is freshly built at that point and
one read beforehand would not be.

Its exit status is a usable signal for this because **mutmut exits zero even
when mutants survive**; a non-zero status means it could not do its job, and
overwhelmingly that means those tests failed.

One gap is proofmark's to fill. mutmut mutates the code under test, never the
tests, so a commit that only touches a test module selects nothing and would be
checked by nothing. Those modules are run directly, first — you find out you
broke the test you just wrote without waiting on mutation.

### How it stays small enough to run every commit

Mutation testing a whole project takes minutes to hours, which no commit hook
can spend. Three things keep this one to roughly the cost of your test suite:

- **It only asks about the functions you changed.** The staged diff gives the
  changed lines, the source gives the function each line sits in, and mutmut is
  asked for those functions by name. A commit that touches no function never
  starts mutmut at all.
- **It needs no coverage report.** Producing one means running the suite under
  coverage instrumentation. Whether a line is covered is mutmut's own question,
  which it answers from its stats cache under `mutate_only_covered_lines`.
- **It reuses verdicts mutmut has already reached.** Naming mutants explicitly
  makes mutmut re-test them, so proofmark does the skipping itself: a function
  whose file has not moved since the mutants were generated is reported from
  the recorded verdict rather than tested again. Without this the bill would
  grow with the branch behind you, since a long-lived branch's diff only
  accumulates.

The one thing it will not reuse is the map the tests above are chosen from —
mutmut's record of which test reaches which function. Not because that map is
unwanted, but because a cached one is wrong: it is rebuilt only for tests
mutmut has not seen before, so editing a function strips its entry without any
test being new, leaving it with nothing to kill its mutants and every one of
them reported as surviving. Those survivors are not real, and they land against
the code you just changed.

So proofmark deletes the record before each run and lets mutmut build it
afresh. That costs one instrumented run of the suite, paid only when a mutant
is actually going to be tested — and it is what makes the tests chosen above
the right ones.

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

A coverage percentage is a ratio, so deleting lines moves it without anything
having got better or worse. Removing well-covered code lowers the project
average — a file at 100% inside a project at 32% drags the mean down when it
goes — and removing uncovered code raises it, having earned nothing.

proofmark records sizes, not just percentages, and asks the question in counts
whenever something shrank: **did any line that was covered stop being
covered?** If not, the change is a deletion and neither gate fires, in either
direction. So dead-code removal never blocks a push.

Nothing is hidden by this. Deleting the only caller of a helper shrinks the
file *and* leaves the helper untested — the uncovered count goes up, and that
still fails. It is only the arithmetic of the ratio that is forgiven.

Sizes are why each file is recorded as `[missing, measured]` rather than a
percentage; the percentage is derived from them, and is exact rather than
rounded.

## Requirements

proofmark installs with no dependencies of its own, but it does not bundle a
toolchain either: it drives `pytest`, `diff-cover` and `mutmut` through
`uv run`, so they are resolved from the dev dependencies of the project being
checked. Set that up once per project:

```bash
uv add --dev pytest pytest-cov diff-cover   # add mutmut for the mutants pass
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
  project. It is only needed for `proofmark commit` and `run --mutants`.

Nothing is silently skipped if a tool is missing. A missing `diff-cover` fails
the run with the command needed to fix it, and a missing `mutmut` warns rather
than letting the mutation pass quietly do nothing.

Keeping proofmark's own install empty is what makes the pre-commit/prek hook
environment a single small wheel that builds in well under a second.

## Use as a hook

There are two hooks, at two stages. Add them to `.pre-commit-config.yaml`, using
whichever remote protocol you normally clone with.

Over HTTPS:

```yaml
repos:
  - repo: https://github.com/chwiese/proofmark
    rev: v0.2.0
    hooks:
      - id: proofmark-commit
        stages: [pre-commit]
      - id: proofmark
        stages: [pre-push]
```

Over SSH:

```yaml
repos:
  - repo: git@github.com:chwiese/proofmark.git
    rev: v0.2.0
    hooks:
      - id: proofmark-commit
        stages: [pre-commit]
      - id: proofmark
        stages: [pre-push]
```

Either hook is useful on its own, and both gate: `proofmark-commit` fails a
commit whose tests fail, `proofmark` fails a push that breaks the coverage
standard. Only the mutation half of the commit hook is advisory.

The two are equivalent, with one caveat if the repository is private: pre-commit
and prek clone hook repositories non-interactively, so HTTPS needs a git
credential helper already configured (`gh auth setup-git` sets one up) or the
clone fails with `could not read Username for 'https://github.com'`. SSH works
as long as your key is set up.

Then install both stages — `pre-commit install` alone covers neither:

```bash
prek install --hook-type pre-commit --hook-type pre-push
# or: pre-commit install --hook-type pre-commit --hook-type pre-push
```

Commits pay for the tests and mutants of what they change; the whole suite and
both coverage gates run on `git push`. A commit that stages no Python skips the
commit hook entirely.

The pre-push hook runs `--check`, which never writes the baseline — a pre-push
hook cannot add a file to the commit you are already pushing. Instead it fails
if the baseline is out of date. Without that it would report success while the
recorded standard quietly stayed at whatever it was first seeded with, gating
nothing.

Recovering costs one command and no second test run: the failing hook has
already written `coverage.json`, so `proofmark check --update` records exactly
the numbers it just showed you. Commit the baseline and push again.

## Use directly

```bash
proofmark run             # run tests, apply gates, raise the baseline
proofmark run --check     # verify without writing (what the pre-push hook runs)
proofmark run --mutants   # also mutation-test the whole tree
proofmark check           # gates only, against an existing coverage.json
proofmark check --update  # gates only, raising the baseline
proofmark commit          # test and mutation-test the staged diff (the hook)
```

`proofmark commit` reads the staged diff, so it reports on what `git commit`
would record — stage your work first. `run --mutants` is the whole-tree sweep,
worth running occasionally to see the backlog the commit-sized pass never looks
at.

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
