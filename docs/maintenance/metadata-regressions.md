# Preventing incomplete metadata regressions

For every reported issue, identify the failure and expand both the regression
suite and the integration checks before qualifying the fix.

- Reproduce the original input and failure in an isolated regression. Add
  closely related positive and negative cases based on the cause. Confirm the
  regression detects the original failure and passes with the fix.
- Exercise the installed plugin through the same Calibre entry point and
  handoff the user used. Check the final data and downloaded assets that reach
  Calibre. Include relevant provider failures, fallback paths, and identity
  conflicts in the regression coverage.
- Run integration through the Metadata Engine core, each adapter on the
  affected path, and every applicable provider. Use the enabled book provider
  inventory for Calibre and include cover sources available to Calibre itself.
  Record the component revisions and each source's result, including outages.
- Record the cause and the added coverage in the pull request. Run both
  suites on the approved Meta workers and retain the exact tested ZIP and its
  qualification evidence before distributing it.

Run the frameworks on `chatgpt-dev` and `chatgpt-build-02`. Keep the test inputs
and entry points in the checkout so the local machines can execute the suites.
Use GitHub Actions for required check orchestration and evidence, with execution
on the self-hosted Meta workers.

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

Give each scenario a fresh copy of provider records because enrichment can
mutate them. Verify that a rejection case reaches the intended provider call
and assert which fields are absent from the final Calibre result.

Validate the final installed-plugin output through the real identify path:
one result for the verified edition, useful subject tags, the correct series
and number, and actual enumerated warning labels when supported by sources.
A referral to an author's website is not a warning list. Do not invent warning
labels or derive them from genre. Retry missing warning details independently
of missing tags; use enabled providers and bounded retries.

Treat broad subjects such as Fiction and Nonfiction as incomplete when a
verified source supplies a more specific genre. Test empty, generic-only, and
already specific subjects separately. Require the expected genre after Calibre's
identify merger and check that an edition with a conflicting identifier cannot
supply it.

For descriptions, verify the displayed layout as well as the text. Exercise
plain text, encoded quotes, inline HTML, and already separated blocks. Render
the installed plugin's output with Calibre's comments converter and Qt, then
check blank lines around each attributed review and preservation of ordinary
dialogue. Keep these deterministic tests mandatory when the live source omits
editorial reviews.

Keep the reported live lookups in `ci/live_quality_check.py`. Require relevant
subjects, actual warning labels, and no duplicate same-ISBN results. Fixture
success and packaging success alone cannot establish live metadata quality.
Failures must remain visible in the required Meta-worker checks.

Compare covers for every live lookup using the identifiers produced by
Calibre's metadata merger. Install checksum-pinned Apple Books, Goodreads and
Kindle cover plugins in the disposable test profile, and run every configured
cover source available there, including Calibre's built-in Google Books source.
Record the decoded dimensions and status for each source. A larger catalog
cover fails the comparison, while general image-search results are recorded
separately because they may show unrelated artwork.
Require known live covers from Kindle, Apple Books, Google Books, Goodreads,
and Open Library. For the Apple Books ebook fixture, use the ISBN result so
the cover of a graphic-novel adaptation cannot set the size target.

Keep the Kindle-only input for `Dark Town` in the live checks. Confirm that
the Goodreads follow-up supplies the synopsis and genres, preserves the
Kindle ASIN, and selects the correct ISBN and `Tales of Temerity` series.

Keep `Every Layout` as an identifier-free query with a missing subtitle
separator, a trailing year and one of two authors. Verify the canonical
title, both authors, the synopsis and relevant tags. An identifier-free
fallback must avoid assigning another edition's cover or publication data.

`Moon Deeds` exercises a series and spelled-out volume embedded in the catalog
title or subtitle. Require independent series and volume corroboration before
cleaning the title or taking genre tags. Keep the Kindle edition identifiers
when the confirming record describes a paperback.

`Spice Box` covers a missing anthology with a semicolon before the subtitle
and alternate contributor credits. Require the exact Kindle ASIN and strong
author overlap for recovery. An included story's series must stay off the box set.

Keep the original decorated Kindle query for `Branding Belle`. Exercise an
empty autocomplete response for the full marketing title, followed by an
exact ASIN discovery and a catalog record with the bare title. Require both
authors, the synopsis and specific genre tags in Calibre. Reject a different
ASIN, conflicting contributor, different base title, or conflicting subtitle.

Keep an identifier-free lookup for the original Bedded by the Headmaster.
Exercise a catalog status prefix on the title, and verify that the original
work still resolves with its series and genres. Reject a different author,
sequel, or rewrite. Keep edition-specific identifiers out of a work-only result.

Keep the saved Goodreads and Kindle identifiers for Henrikk's Punishment.
Exercise a Goodreads record that is unavailable and require an exact title and
author in a separate bibliography before returning the work and series.
A live saved record with a conflicting ASIN, author, or title must be rejected.
A bibliography that establishes only the work cannot supply a new edition ID
or cover.

Keep the original ISBN for `Star Wars: Knight Errant`. Cover a correct
edition with an existing synopsis and missing or generic subjects, plus
catalog timeouts and a separate comic by the same author. Exercise Open
Library's edition response with the work title and franchise in separate
fields. Require a relevant genre after Calibre's merger. Preserve the selected
ISBN and synopsis, and reject subjects from mismatched ISBNs and contributors.

When an edition lookup fails, retry book catalogs by title and author. Only
confirmed paperback and hardcover records can supply fallback work metadata.
Test the title and author request through the provider endpoint, including a
catalog that moves the franchise label after the book title. Verify the
synopsis, tags and identifiers in Calibre, and reject adaptations, audiobooks,
unknown bindings and author conflicts. Keep the print edition's identifiers,
publisher, date and cover out of the fallback result.

The same live ISBN lookup must return `Star Wars: Knight Errant` #1.5. Reproduce
the Goodreads ISBN redirect and exact edition response in Calibre. A numbered
series can be supplied independently of tags. An unnumbered franchise
collection, a conflicting title or author, or a different ISBN cannot supply
the series. Preserve the edition's existing description, publisher and tags.

Test the original `Star Wars - 201 - Shadows of the Empire` query with Steve
Perry and ISBN `9780553574135`. A middle catalog index may narrow discovery,
but cannot set the series position. Check the ISBN and author against bare
and franchise-qualified catalog titles, and reject wrong ISBNs, authors and
titles. Without an ISBN, require a catalog franchise label as well as the
work title and author. Preserve literal titles when the shortened search
misses. The verified Goodreads collection for this edition has no position.

Exercise the cover choices for `Constance Verity Destroys the Universe` with
the ISBN, Kindle ASIN and Google Books volume ID. Check decoded images across
sources and resolutions. Keep one highest-resolution copy of each distinct
artwork. A mostly white title page is a fallback when no illustrated cover
is available, in both the chooser and automatic selection. Compare cover
quality using artwork when catalogs return both artwork and title pages.

Use the original Calibre sort title `Element Encyclopedia of the Psychic
World, The`, inverted author `Cheung, Theresa`, and Kindle ASIN
`B004FPYX40`. Verify reading-order search queries and both catalog title
forms, including the longer subtitle. Reject a different Kindle ASIN,
title or author, and keep the paperback ISBN off the Kindle record.

For distribution, qualify the exact deterministic ZIP with the existing Docker
integration workflow. Inspect all mandatory job outcomes and import the original
qualification record. Source or README changes invalidate the ZIP digest.
Never edit a digest or mark an untested artifact qualified merely to make CI
green. Changes to tests and maintenance documentation do not change the ZIP's
four declared members.

The current regression covers `Wicked Darlings 1: A Court of Sugar and Spice`
alongside `Spice Travels` and `Smoke & Spice`. These are test cases, not
production title-specific exceptions.
