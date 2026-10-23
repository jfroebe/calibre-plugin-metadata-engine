# PEP 8 maintenance audit: calibre-plugin-metadata-engine

Baseline: `be57092b551d995072f3c016e483f48fb6e59a53` on `main`; 19 Python files.
Ruff linter and formatter: 0.14.1. No maintained Python files are excluded.
External `.ci` dependency checkouts are excluded from candidate linting.

| Initial rule | Findings |
| --- | ---: |
| E501 | 26 |
| E731 | 1 |
| I001 | 18 |
| N802 | 4 |
| N999 | 1 |

Initial audit findings: 50, including E501 observations.
Selected lint findings before maintenance: 24.
Final selected lint findings: 0. Formatting check passes.
Remaining observed E501 findings at 88 columns: 1.
See PEP8.md for the explicit formatter line-length policy and public-contract
exceptions. This is compliance with documented exceptions, not strict PEP 8.

Formatting preserved every complete AST. Import organization preserved every
non-import AST. Import blocks were reviewed for local-module, Calibre/Qt,
optional-dependency and fixture initialization behavior; imports were never
moved across setup statements. Delayed smoke-script imports retain their order.
Unused stdlib imports and duplicate names from modules still imported elsewhere
were removed after review. Unsafe fixes were not enabled. Local lambda helpers
became equivalent nested functions; Skyhook's unused value retains its original
evaluation under an underscore-prefixed local name.

## Stage validation

- Tooling: baseline suites and configuration inspection; source untouched.
- Formatting: Ruff format check, complete AST equality, compileall, local suites.
- Imports: focused I/E401 checks, non-import AST equality, compileall, local suites.
- Naming: focused N checks; public exception/callback names preserved.
- Residual style: full selected lint/format checks, compileall, local suites.
- CI: deliberate invalid lint/format probes each fail with exit status 1.

Local suites: engine 266 passed / 3 environment skips; plugins 423 passed plus
26 subtests; adapters 244 passed plus 34 subtests and 2 pre-existing warnings.
Calibre host packaging (3) and actual-engine proxy (5) tests pass; ordinary
pytest at its package root requires Calibre and is not its host-test entry point.
The existing `ci/unit_report.py` entry point is used instead.
Worker PostgreSQL, packaged runtime, native Plex and real Calibre gates remain
required before declaring cross-repository completion. No tests were removed
or weakened. No production services were modified and no image was published.
