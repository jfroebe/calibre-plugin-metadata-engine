# Local Calibre library acceptance

The installer places the runner under
`~/.local/share/calibre-library-acceptance/current` on both Meta VMs. It starts
no service or scheduled job. Use the local agent account and an installed
official Calibre on the worker that can read both the snapshot and ebook share.
The controller can invoke that worker over the existing SSH connection.
The worker owns the queue, checks, retry timing, and checkpoints.

## Freeze a sample

Use a consistent copy of Calibre's production `metadata.db` and a read-only
mount of the book files. Use a private directory outside both source paths.
The CLI opens SQLite in read-only mode and opens ebook files for reading.

```bash
runner="$HOME/.local/share/calibre-library-acceptance/current/library_acceptance.py"
state="$HOME/.local/state/calibre-library-acceptance/pilot-1"
python3 "$runner" --state "$state" --calibre-bin /opt/calibre prepare \
  --database "$SNAPSHOT_DB" --books "$READ_ONLY_BOOK_ROOT"
```

The default sample is up to 20 books per group, ordered by a stable seed and
stratified by ISBN, ASIN, and text-only inputs. A saved `metadata_engine_*`
identifier is evidence of prior processing. Absence of that identifier leaves
prior processing unknown. Separate cases are recorded when a file's embedded
title, authors, or identifiers differ. The extraction inventory records formats
that matched the catalog query or could not supply an embedded title. A seed
or sample size change requires a fresh state directory.

## Start a run explicitly

Use an accepted plugin ZIP and the candidate ZIP. To build a candidate from a
patch or commit, use the repository's `python3 ci/package.py` locally. Set
`CALIBRE_ACCEPTANCE_BASELINE_TOKEN` and `CALIBRE_ACCEPTANCE_CANDIDATE_TOKEN` in
the worker's environment if the engine endpoints require bearer credentials.
Use the worker's existing proxy environment and put the LAN engine hosts in
`no_proxy` and `NO_PROXY`. Record the actual deployed image digest or component
revision for each endpoint. If comparing plugin-only changes, both endpoints
may be the same. Use separate test deployments when changing the engine.

```bash
python3 "$runner" --state "$state" --calibre-bin /opt/calibre run \
  --name pilot \
  --baseline-zip "$BASELINE_ZIP" --candidate-zip "$CANDIDATE_ZIP" \
  --baseline-engine "$BASELINE_URL" --candidate-engine "$CANDIDATE_URL" \
  --baseline-revision "$BASELINE_REVISION" --candidate-revision "$CANDIDATE_REVISION"
```

Each attempt uses its own Calibre profile. The runner defaults to two concurrent
cases. Baseline and candidate lookups for the same case run sequentially.
Transient failures get up to two retries by default. The same command resumes
interrupted work. Add `--retry-failed` to rerun completed non-passing cases on
the same pinned inputs. Each attempt is preserved.

To check a new candidate against failed cases, use a fresh `--name` with
`--failed-from pilot --reuse-baseline pilot`. Add `--replay pilot` to serve
captured responses locally from the previous candidate. Replay requires
matching request URLs and reports missing captures as unverified. A replay pass
is diagnostic evidence. Run the targeted cases live and then run the full
manifest live with the same accepted baseline before accepting a change.

## Read the reports

`summary.json` contains case IDs, outcome counts, failure groups, retries,
and build fingerprints. Retrieve it over the existing SSH path for review in
ChatGPT. `report.local.json`, ebook metadata, images, provider responses, and
logs stay in the private state directory. No results are uploaded automatically.
Use the local report, candidate list, and provider log to trace failures. A
successful full live run requires a pass for every selected case.

Matching checks the work and any supplied ISBN or ASIN first. A missing
synopsis, tags, series position where expected, or usable cover fails. Changes
to previously saved tags, a substantially shorter synopsis, and unfamiliar
cover art can require review. A mostly white cover is flagged for review when
there is no reference and fails if an illustrated reference exists. The accepted
baseline and frozen catalog may differ because the catalog can include edits
or other sources. Use optional `--review /private/path/decisions.json` for
verified title aliases, cover and synopsis hashes from the local report, and
approved tag removals. The file maps a case ID to optional arrays `titles`,
`cover_sha256`, `synopsis_sha256`, and `dropped_tags`. Decisions are pinned
with each run. Keep the state directory for as long as you need its baseline
and replay evidence.
