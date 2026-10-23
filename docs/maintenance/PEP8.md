# Python maintenance conventions

Install `python -m pip install -r requirements-style.txt`, then run
`python -m ruff check .` and `python -m ruff format --check .`.
Ruff 0.14.1 is both linter and formatter; no additional formatter is required.
The syntax target is Python 3.11, preserving adapter minimum compatibility.
Four spaces, UTF-8, LF endings and an 88-character formatter target apply to
all maintained Python files, including tests and scripts. `.ci` contains
external dependency checkouts and is excluded from this repository's checks.

Selected checks cover E4/E7/E9, whitespace (W), import grouping (I), naming (N),
and unused imports/locals (F401/F841). Calibre retains its existing F/D/UP
checks, with their original scope for D/UP. PEP 257 and API migrations are
outside this initiative.

## Explicit deviation from strict PEP 8

Line length is enforced by the formatter's 88-character target, not E501.
Ruff does not split every literal or comment: embedded SQL, HTML/JavaScript,
provider queries and serialized text must preserve their values. The initial
audit also measures E501 to make this remaining deviation visible; it is not
part of the selected lint gate. This is compliance with documented exceptions,
not a claim that every physical line meets PEP 8's 79-character recommendation.

Public identifiers, callback names, discovery/import contracts, configuration
keys and schemas must remain unchanged. Import side effects and delayed
imports need individual review. Unsafe autofixes are not permitted by default.
The baseline reports distinguish formatter, imports, naming, unused bindings
and behavior-sensitive findings. Existing test failures must be compared with
the recorded pre-remediation results.

## Naming compatibility

The four camel-case `unittest.TestResult` callbacks in `ci/unit_report.py` keep
their required names, with individual N802 exceptions. The root `__init__.py`
is the Calibre ZIP entry point; its repository directory name contains hyphens.
The N999 exception applies only to that ZIP entry point.

## Integrated Calibre reconciliation

PR #3 now incorporates main at `80c23f429592cdc69ad40b3e32543e4177852fb5`, including Calibre 1.1.3 media/schema/edition fixes, all 17 runtime regressions, independent desktop acceptance, PostgreSQL readiness and exact-artifact qualification/distribution. The audit covers all 26 Python files. After excluding imports and normalizing the reviewed E731 lambda-to-function spelling, all non-import ASTs match integrated main. The style job uses a hosted runner because this public repository has no registered local worker. Formatting changes ZIP bytes; old artifact evidence is retained as history but cannot approve this candidate. New Docker-backed qualification must pass before reconciliation is complete.

Refreshed full qualification passed in [Docker run 40006214502](https://github.com/jfroebe/metadata-engine-docker/actions/runs/40006214502) for source `f8d9a1043b44905c15ec67dd5664d1f209d3c5b3`. Both native platforms, offline Calibre/engine/library, SQLite/PostgreSQL persistence and authenticated proxy gates passed. The original qualification record is in `ci/release-evidence.json`; ZIP SHA-256 is `6cfd7b1681afa099943dc00edef30a205d8fcb5d10014613625bea954e843a43`. Subsequent audit/evidence updates preserve those source ZIP bytes.
