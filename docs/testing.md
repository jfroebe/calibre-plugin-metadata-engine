# Calibre integration qualification

The test environment uses the existing Linux workers `chatgpt-dev` and
`chatgpt-build-02`. Windows 11 desktop and macOS desktop were independently
validated and accepted by the repository owner on October 23, 2026. Neither
desktop is required in this environment. Hosted Windows Server regression
coverage remains useful and does not require a permanent Windows VM.

Personal Calibre libraries, production databases and production services are
never test inputs. Every runtime has disposable configuration, cache and library
paths. The existing declaration of Calibre 6 as a minimum is retained; the
current automated runtime coverage is Calibre 9.15 and 9.16.

## Required integration checks

| Check | Execution |
|---|---|
| Packaging and source identity | Deterministic ZIP, exact member bytes, namespace marker, CRC, version agreement and digest |
| Native installed-plugin regressions | Pinned official Calibre 9.15 / bundled Python 3.14.7 on hosted Linux and Windows |
| Upgrade preservation | Set every existing option through the previous installed ZIP, upgrade, read in a fresh Calibre process, remove and reinstall |
| Metadata/API/cover contracts | Seventeen actual-Calibre regressions, including mixed book/movie results, incompatible JSON structures, exact-edition full-resolution artwork and provider failures |
| Client proxy behavior | Actual inherited HTTP forwarding, loopback NO_PROXY bypass, failed proxy connection without cache deletion, and subsequent cached-cover recovery |
| Engine persistence | Actual HTTP API and cached metadata survive restart and an uncached provider failure, using both SQLite and disposable PostgreSQL |
| Engine network behavior | Deterministic environment/explicit-proxy routing, authenticated live HTTP/HTTPS/SOCKS, rejected authentication and connection failure; SOCKS remote-DNS test forbids local destination resolution |
| Engine/container/library integration | Pinned LinuxServer Calibre 9.16 with actual pinned engine, repeated-query cache reuse, OPF/cover retrieval, disposable-library writes and query verification |
| Combined component baseline | Verify the successful engine/plugins/adapters/PostgreSQL snapshot against exact recorded component revisions |

The fault API in `tests/fixture_api.py` exercises controlled failures without
replacing the real engine integration. `tests/engine_service.py` imports the
actual pinned engine routes, provider manager, cache and evidence ledger. Only
its fixture provider and control endpoints are test-specific.

A movie carrying a book's ISBN must not win deduplication or supply its cover.
Explicitly non-book media are excluded; untyped legacy book records remain
compatible. Structurally incompatible JSON produces an actionable schema error
while healthy providers may continue. A known conflicting ISBN/ASIN cover is
excluded rather than silently used as fallback artwork.

## Worker and repository boundaries

`chatgpt-build-02`'s Actions runner belongs to `metadata-engine-docker`. The
controller's existing Docker-repository runner retains the `meta-controller`
label but verifies hostname `chatgpt-dev`; the old VM remains deleted. Do not
register another runner or recreate a VM merely to match a label.

The full integration workflow is
`jfroebe/metadata-engine-docker/.github/workflows/validate-calibre.yml`.
It accepts an exact reviewed Calibre commit, builds its ZIP once, and passes
that artifact to every runtime. Hosted jobs use ordinary hosted networking;
local workers use the existing HTTP relay at `192.168.0.59:8118`.

Private engine access uses the existing `METADATA_ENGINE_REPO_TOKEN` in the
Docker repository. Live SOCKS checks use its existing
`METADATA_ENGINE_TEST_SOCKS5_USERNAME` and
`METADATA_ENGINE_TEST_SOCKS5_PASSWORD` secrets. Secrets are not added to the
public Calibre repository or copied into reports. The public workflow no longer
targets workers or credentials unavailable to that repository.

PostgreSQL tests start a uniquely named, labeled container from the controller's
already-cached image, with no production data mounts and a random loopback port.
The fixture creates a separate generated database per test work directory.
Cleanup removes only the owned container and its anonymous volumes. Existing
`meta-postgres` is inspected for its cached image identity and is never modified.
Tests wait for actual engine cache readiness before checking persisted results;
API liveness alone is insufficient for asynchronous PostgreSQL startup.

The build-worker Calibre container has no external network, no capabilities, no
host GUI or Docker socket, read-only source inputs and disposable configuration.
The actual engine runs on private container loopback. The container is removed
on success and failure. Controller virtual environments are unique per run and
removed afterward. Jobs have bounded timeouts.

## Verified component baseline

[Snapshot run 39944551219](https://github.com/jfroebe/metadata-engine-docker/actions/runs/39944551219)
succeeded at Docker revision
`c4f3453f72091c10af0f9168525c9e8af0b05e30`. It validated:

- Engine `6d8d759428cf8049f5b8f4c4894022b897fd3229`: 269 tests passed.
- Plugins `81b3f71febc79491c1cb0b59487bb551b255c14d`: 423 tests passed.
- Adapters `9f7732138c72723f10a5a5aafaaa3d1a4e6282d1`: 235 passed, 9 skipped.
- PostgreSQL-dependent gates, extension contracts and generated documentation.

`ci/snapshot_gate.py` retrieves that exact run with the current Docker workflow's
read-only token. It checks success, workflow path, head revision and every
component against the manifest at that revision. An unrelated green run cannot
qualify different components. Calibre is then tested against that same engine
pin; the full historical suite is not needlessly repeated.

## Qualification and distribution

Normal Calibre PR/push CI requires packaging and both native runtime jobs.
Reusable calls default to `mode=release`; release mode additionally requires
`ci/release_gate.py` to verify reviewed integration evidence. Native success
alone does not approve distribution.

The Docker integration workflow rejects failed, cancelled or skipped mandatory
jobs. On success it retains the exact tested ZIP with `qualification.json` as a
30-day Actions artifact. That record binds the source/workflow revisions, ZIP
digest, component/runtime pins, snapshot evidence, independent desktop
acceptance and required successful job outcomes.

After inspecting a successful run, its original record is committed as
`ci/release-evidence.json` and the compatibility status can become `qualified`.
The artifact digest and all component/runtime/desktop records must still match;
changing a status flag alone cannot approve a release. Source changes that alter
the ZIP invalidate the old evidence and require a new integration run.

`.github/workflows/build-plugin.yml` now waits for release-mode validation and
commits the downloaded tested candidate ZIP. It does not rebuild a different
archive after testing. The ZIP must match both the checkout and the recorded
integration digest. This workflow distributes the installable repository ZIP;
it does not publish a tagged release or deploy production services. Existing
project-wide formal release and quality checks remain separate.

## Running focused checks

Use a disposable checkout and an isolated host Python environment:

```bash
python3 -m venv /tmp/calibre-tests-venv
. /tmp/calibre-tests-venv/bin/activate
python -m pip install -r ci/requirements.txt -r /path/to/pinned-engine/requirements.txt
python -m ruff check ci tests
python -m ruff format --check ci tests
python ci/unit_report.py --pattern test_package.py --require-no-skips --report reports/package.xml
CALIBRE_ENGINE_ROOT=/path/to/pinned-engine \
  python ci/unit_report.py --pattern test_engine.py --require-no-skips --report reports/engine.xml
```

Use `ci/unit_report.py`, rather than root pytest discovery, because the plugin's
root imports genuine Calibre APIs. Zero collected tests and requested no-skip
suites fail the harness. Windows checkouts preserve LF source bytes through
`.gitattributes`, so ZIP/source comparison remains exact across platforms.

For an existing authorized local worker:

```bash
python ci/package.py
python ci/container_runtime.py \
  --engine-root /path/to/pinned-engine \
  --previous-zip /path/to/previous/Calibre-Metadata-Engine.zip \
  --report reports/linux
```

## Private Linux GUI controls

GUI acceptance passed on both existing workers in
[run 39849014743](https://github.com/jfroebe/chatgpt-dev-control/actions/runs/39849014743):
typed input, real Qt button clicks, key events and valid PNG screenshots. The
owned desktop containers and generated passwords were removed afterward.

`ci/gui_control.py` provides `start`, `windows`, `click`, `type`, `key`,
`screenshot` and `stop`. It refuses containers lacking its ownership label.
Selkies HTTPS binds only to worker loopback; password files stay outside report
artifacts. Automated control on the build worker uses the controller's existing
restricted SSH command path. That management key does not permit forwarding;
do not weaken it. Interactive browser access requires an independently
authorized forwarding identity or VM console. No public tunnel is configured.

## Acceptance history

- [Native run 39953556989](https://github.com/jfroebe/calibre-plugin-metadata-engine/actions/runs/39953556989): earlier ten-test suite and preference preservation passed on Calibre 9.15 / Python 3.14.7, Linux and Windows.
- [Client run 40001131363](https://github.com/jfroebe/calibre-plugin-metadata-engine/actions/runs/40001131363): expanded seventeen-test client contract suite passed on both native platforms.
- [Integration attempt 40002102066](https://github.com/jfroebe/metadata-engine-docker/actions/runs/40002102066): exact baseline, native Linux, offline container/actual-engine/library integration, SQLite persistence and deterministic engine proxy routing passed. Windows byte comparison exposed CRLF checkout conversion, now fixed with LF checkout rules. PostgreSQL restart testing exposed the harness's premature API-only readiness check, now fixed by waiting for cache readiness. This attempt does not count as full qualification.

Final successful integration evidence is recorded separately in
`ci/release-evidence.json` once all required jobs pass.
