# Preventing incomplete metadata regressions

A successful HTTP lookup or a matching ISBN does not establish that the final
Calibre result is complete. Catalogs may split a series heading into `title`
and the actual book name into `subtitle`; another catalog may supply the same
work as a plain title with a marketing subtitle. A nameless series object with
position zero supplies no series evidence.

For matching changes, retain regression inputs shaped like the actual provider
responses. Exercise both title arrangements, punctuation differences in author
names, missing series names, and every source ordering. Keep negative cases for
conflicting authors, work titles, ISBNs, and series positions. A shared ISBN must
not override a contradictory identity.

Validate the final installed-plugin output through the real identify path:
one result for the verified edition, useful subject tags, the correct series
and number, and actual enumerated warning labels when supported by sources.
A referral to an author's website is not a warning list. Do not invent warning
labels or derive them from genre. Retry missing warning details independently
of missing tags; use enabled providers and bounded retries.

Keep the reported live lookups in `ci/live_quality_check.py`. Require relevant
subjects, actual warning labels, and no duplicate same-ISBN results. Fixture
success and packaging success alone cannot establish live metadata quality.
Failures must remain visible in the required Meta-worker checks.

For distribution, qualify the exact deterministic ZIP with the existing Docker
integration workflow. Inspect all mandatory job outcomes and import the original
qualification record. Source or README changes invalidate the ZIP digest.
Never edit a digest or mark an untested artifact qualified merely to make CI
green. Changes to tests and maintenance documentation do not change the ZIP's
four declared members.

The current regression covers `Wicked Darlings 1: A Court of Sugar and Spice`
alongside `Spice Travels` and `Smoke & Spice`. These are test cases, not
production title-specific exceptions.
