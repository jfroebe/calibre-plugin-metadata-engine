# Calibre integration testing

## Current assessment

**Implemented test infrastructure; not fully release-qualified.** No release is published or
deployed by this change. Personal Calibre configurations and libraries are
never used. The existing plugin implementation remains unchanged.

The previous workflows checked syntax and ZIP creation but did not run Calibre.
There was no test directory or established runtime regression suite. Both jobs
repeated packaging checks. `meta-controller` remains in other current workflow
files, including a Docker snapshot job that explicitly requires hostname
`chatgpt-dev`. This is evidence that the label may have survived the deleted VM;
it is not verified runner inventory. Do not recreate that VM or rename labels
based only on their spelling.

`ci/compatibility.json` records the exact component SHAs from Docker's checked-in
snapshot at `2c61e895be519a32b3e1e5028eaca6355663a4bb`. That snapshot is a
**candidate**, not a verified last-compatible baseline. Successful historical
cross-repository workflow evidence has not been retrieved. No shelved PR is
merged, rebased or restored. The plugin-under-test revision is the current
workflow SHA, recorded with the ZIP digest in `environment.json`; the previous
ZIP is from the exact historical Calibre commit in the manifest.

## Implemented layers

1. `ci/package.py` builds a deterministic root-level ZIP, verifies every member
   against the checkout, checks CRCs, required namespace marker, UTF-8, syntax,
   merge markers, whitespace and plugin/HTTP version agreement. Packaging
   regressions reject missing-marker and stale-source archives.
2. `ci/run_runtime.py` invokes real `calibre-customize`, `calibre-debug`,
   `fetch-ebook-metadata` and `calibredb`. It creates private temporary
   configuration, cache, temporary and library paths. It installs the prior ZIP,
   installs the candidate, checks registration, runs namespace/configuration and
   metadata/cover fault tests, removes and reinstalls the candidate.
3. `tests/engine_service.py` loads the pinned engine entry point and the engine's
   actual provider loader, HTTP routes, serialization, cache and evidence
   machinery. A test-only book provider serves deterministic records. It does
   not replace engine HTTP handlers or call external metadata providers.
4. The runtime harness fetches OPF metadata and a generated cover through Calibre
   and the actual engine, writes them to a synthetic disposable library, and
   verifies title, ISBN and provenance with `calibredb`. A provider-call counter
   proves repeated metadata requests use the engine cache.

`tests/calibre_runtime.py` requires actual Calibre; it does not stub Calibre
imports. Its fault API is explicitly separate from actual-engine validation.
Fixtures include The Hobbit, two Dune editions, The Martian book, Interstellar
novelization, Unicode, an unknown title and a minimal record. Metadata mapping,
provider selection, disabled discovery, outage isolation, invalid JSON, 401/404,
timeout/abort, cover fallback, image signatures and confidence/provenance have
assertions. These runtime tests passed against the pinned LinuxServer Calibre 9.16 image.

## Running locally

Use a disposable development checkout. Python test dependencies are isolated
from Calibre's bundled Python:

```bash
python3 -m venv /tmp/calibre-tests-venv
. /tmp/calibre-tests-venv/bin/activate
python -m pip install -r ci/requirements.txt -r /path/to/pinned-engine/requirements.txt
python -m ruff check ci tests
python -m ruff format --check ci tests
CALIBRE_ENGINE_ROOT=/path/to/pinned-engine \
  python ci/unit_report.py --report reports/host.xml
python ci/install_calibre.py /path/to/disposable-tools/calibre-9.15.0/bin
python ci/package.py
python ci/run_runtime.py \
  --calibre-bin /path/to/disposable-tools/calibre-9.15.0/bin \
  --zip Calibre-Metadata-Engine.zip \
  --engine-root /path/to/pinned-engine \
  --previous-zip /path/to/previous/Calibre-Metadata-Engine.zip \
  --report reports/linux
```

Use `ci/unit_report.py` rather than pytest discovery from the repository root:
pytest imports the plugin root `__init__.py` in a host Python without Calibre.
The host suites use unittest and emit JUnit XML without impersonating Calibre.

The Linux official archive is pinned by version, URL and SHA-256; immutable
archive bytes can be cached. The archive digest was measured after downloading
from the official distribution endpoint. It is not an independent signature
verification. Calibre executables must already have their normal OS libraries
available on the build worker.

## Local CI setup and credentials

The workflow preflight requires verified JSON arrays in repository variables:

- `CALIBRE_CONTROLLER_RUNNER_LABELS`: labels selecting existing `chatgpt-dev`.
- `CALIBRE_BUILD_RUNNER_LABELS`: labels selecting existing `chatgpt-build-02`.

Both arrays must include `self-hosted`, `linux`, `x64` and enough existing labels
to select the intended worker. Runtime jobs verify actual hostnames before doing
work. Obtain inventory from GitHub Actions settings or its authenticated runner
API; do not guess labels. The GitHub connector available during implementation
rejected the runner-inventory endpoint.

The existing Docker-repository token and runner were successfully used by the
companion targeted runtime workflow. The worker-label variables above apply to
the still-provisional Calibre-repository workflow, not to the tested Docker
workflow, which uses its established placement labels directly.

Required existing/secure credentials:

- `METADATA_ENGINE_REPO_TOKEN`, with Contents read access to the pinned component
  repositories. Git checkouts disable credential persistence. A missing token
  fails preflight immediately rather than leaving jobs queued.
- `CALIBRE_TEST_SOCKS_PASSWORD`, supplied securely for the existing authenticated
  relay. The cloud implementation environment had no accessible value. Existing local
  protected credentials may be used by a separate proxy gate; never commit the
  value or put it in a workflow dispatch input.

Local worker downloads use HTTP/HTTPS `192.168.0.59:8118`; loopback engine
requests bypass proxies. `ci/proxy_gate.py` tests engine Runtime HTTP forwarding,
HTTPS CONNECT, authenticated SOCKS5, `socks5h`, invalid authentication and a
controlled connection failure. The six relay requests are opt-in qualification,
not repeated basic infrastructure troubleshooting. Logs only retain sanitized
outcomes for these checks. Host tests separately cover explicit HTTP forwarding,
environment inheritance, NO_PROXY, child environment isolation and bounded
connection failure without using the real relays.

SOCKS qualification here exercises the **engine Runtime**, not the Calibre
client's urllib transport. Do not infer that urllib accepts SOCKS proxy URLs.
Full client/engine proxy inheritance and cache-preservation qualification still
requires completion. Remote-DNS mode is requested with `socks5h`; independent
proof that no local DNS lookup occurred remains outstanding.

Fork PRs run hosted packaging only. Persistent worker jobs never run untrusted
fork code. Same-repository branches use the existing runner trust boundary.
Every job has a timeout; temporary engines stop on success and failure, and
per-run virtual environments are removed. Calibre libraries/configuration are
removed by `TemporaryDirectory`; useful diagnostics remain in `reports/` and CI
artifacts for 14 days. Distinct runs never share a test database or library.

## Compatibility matrix

| Environment | State |
|---|---|
| Host Python 3.12, Linux x86-64 | Packaging, actual-engine API/cache and deterministic routing tests passed locally |
| Calibre 9.15.0 official Linux x86-64 | Native hosted runtime, ten regressions and preference-preserving upgrade passed in run 39953303726 |
| Calibre 9.16.0 / bundled Python 3.14.8 | Ten regressions and actual-engine/library integration passed in the pinned container |
| chatgpt-build-02 | Container launch, ten regressions, engine/cache/library integration and GUI control passed |
| chatgpt-dev | Container launch, ten real-Calibre regressions and GUI control passed |
| Windows Server 2025 / pinned Calibre 9.15.0 | Ten regressions and preference-preserving upgrade passed; bundled Python 3.14.7 |
| Windows 11 desktop/current Calibre | Remains outside hosted Server regression coverage |
| Calibre 6.0 declared plugin minimum | Existing declaration retained; not verified by these tests |
| macOS | Future extension; no support claim added |

The downloaded archive is `calibre-9.15.0-x86_64.txz`. Both extracted launchers
are Linux ELF x86-64 binaries. The environment allows `/proc/self/exe` but denies
`/proc/<numeric-pid>/exe` even for the invoking process, consistent with the
observed launcher error. That is evidence of a runtime-environment constraint,
not proof the plugin fails under a normal Linux Calibre installation.

## Remaining release blockers

- Retrieve successful historical cross-repository runs and select the last
  mutually compatible baseline; the candidate manifest alone is insufficient.
- Finish the Calibre repository's PR/release orchestration using the companion
  Docker-repository runtime workflow and the same tested ZIP artifact. The
  existing build-worker runner is scoped to the Docker repository; do not guess
  that it can accept Calibre-repository jobs. The actual runtime harness has
  passed; the draft Calibre-repository qualification job remains provisional.
- Complete Windows 11 desktop/current-version qualification beyond the pinned
  native Windows regression job added here. No permanent Windows VM is required.
- Verify future preference schema migrations when such migrations are introduced;
  current-schema preference preservation is now asserted across ZIP upgrades.
- Verify mixed book/movie responses and wrong-edition/full-resolution covers;
  the existing book fixtures alone do not prove media filtering.
- Add structural API schema rejection assertions; malformed JSON coverage does
  not prove incompatible-but-valid JSON fails actionably.
- Finish proxy failure/cache persistence and client-routing assertions, and
  execute authenticated relay checks securely on the existing workers.
- Cross-check all pinned providers/adapters/Docker together through the existing
  snapshot process, including PostgreSQL where required. The small SQLite
  engine integration test is not a substitute for that gate.
- Complete source-wide PEP 8/257, dependency/deprecation and secret-scanning
  validation; current lint covers new test/harness files only. PEP 249 applies
  to the engine implementation; this harness issues no direct SQL.

### CI aggregation and full release qualification

Normal PR and push runs require packaging plus native Linux
and Windows Calibre 9.15.0 tests. Both operating systems consume the same ZIP
artifact from the packaging job. The Windows MSI and Linux archive are pinned
by version and SHA-256 and extracted into temporary directories. Windows uses a
GitHub-hosted Windows Server 2025 runner, not a Windows 11 desktop qualification.
The Windows installer digest was measured from the official download, not an
independent publisher signature. Test reports retain Calibre and bundled Python
versions; no current-version claim follows from a moving installer.

The lifecycle harness seeds every existing option through the prior installed
plugin, upgrades to the candidate and reads the persisted options in a fresh
Calibre process. Any changed or lost value fails the suite. This qualifies
preservation of the current preference schema; no nonexistent schema migration
is inferred. Existing installed-plugin regressions and removal/reinstallation
still run after that assertion.

The final `qualification` job aggregates actual results. Missing, failed,
cancelled or skipped mandatory jobs fail it. It no longer contains an
unconditional failure after successful tests. Hosted jobs do not inherit private
LAN proxy settings. Local jobs retain their relay settings.

Dispatch with `mode=local` additionally requires the existing engine, proxy and
container jobs and their worker-configuration preflight. Those runner variables
and credentials are still unavailable in this repository; the Docker-repository
integration workflow remains the working route to the build worker. Normal CI
does not pretend that another repository's runner can accept its jobs.

Reusable calls default to `release` so an existing release caller cannot silently
receive narrower CI approval. A caller may explicitly request `mode=ci` for
packaging and native regression validation.

Dispatch with `mode=release` requires all local and native jobs plus a
`qualification_status` of `qualified` in the exact compatibility manifest. That
status remains `candidate-unqualified`. Change it only after the remaining
release evidence listed above has been completed and reviewed. A green normal
CI run is not full release approval. No release publication is added here.

## Verified runtime prerequisites

The official Linux download page requires GLIBC >= 2.34 and libstdc++ >=
6.0.30. This implementation environment has GLIBC 2.39 and libstdc++ 6.0.33.
EGL/OpenGL and Xinerama are present. `libxcb-cursor.so.0` is missing, which
blocks the Qt XCB platform plugin; the offscreen platform plugin and bundled
launcher have no unresolved shared-library dependencies when checked with the
installation's library directory. The archive includes libpython3.14.so.1.0.

`ci/runtime_preflight.py` records these checks and numeric `/proc/PID/exe`
readability before runtime testing. The Linux CI job retains its report.
A GUI/XCB test environment needs libxcb-cursor0; the headless test harness uses
QT_QPA_PLATFORM=offscreen. Missing XCB is separate from the observed early
launcher error.

Official reference: https://calibre-ebook.com/download_linux

## Pinned LinuxServer container runtime

The default Linux runtime now uses
`ghcr.io/linuxserver/calibre@sha256:12013dbe3328eb1b1d72fcbab5ce6d321df68272dfd775082b996d8f54a37e0c`
(linux/amd64, LinuxServer `v9.16.0-ls428`, Calibre 9.16.0). The digest was resolved
from the registry's platform manifest, and version labels checked. This adds
current-version coverage; it does not retroactively qualify the 9.15.0 regression.

`ci/container_runtime.py` builds a cached local test image from that immutable
base plus the exact engine dependency input. Only the build uses the configured
web proxy. The actual test container has `--network none`, a private loopback
engine, no capabilities, no host GUI/Docker socket, temporary configuration,
and read-only test inputs. It mounts only the reports directory writable.

```bash
python ci/package.py
python ci/container_runtime.py \
  --engine-root /path/to/pinned-engine \
  --previous-zip /path/to/previous/Calibre-Metadata-Engine.zip \
  --report reports/linux
```

## Controlling the Linux GUI on both existing workers

Use container desktops on `chatgpt-dev` and `chatgpt-build-02`, rather than
installing permanent host desktop services. The existing control Actions runner
runs on `chatgpt-dev`; it can use the controller's existing restricted SSH key
to execute the same commands on `chatgpt-build-02`. No Docker TCP socket or new VM
is needed. A runner registered to the Docker repository is not available to
jobs in the Calibre repository; runtime orchestration must respect that boundary.

```bash
python ci/gui_control.py start --report /path/to/private/gui-session --port 18181
python ci/gui_control.py windows --name <reported-container>
python ci/gui_control.py click --name <reported-container> --x 400 --y 300
python ci/gui_control.py type --name <reported-container> --text 'test query'
python ci/gui_control.py key --name <reported-container> --key Return
python ci/gui_control.py screenshot --name <reported-container>
python ci/gui_control.py stop --name <reported-container>
```

The GUI is a real X11 desktop (`PIXELFLUX_WAYLAND=false`) with xdotool and Qt screen capture.
The tool refuses to control containers lacking its ownership label. Browser
access uses Selkies HTTPS, bound **only to worker loopback**, with a generated
password outside artifact directories. `session.json` records the container,
port and password-file location but not the password. Do not upload that
password file. Remove it after ending a session.

From a machine with existing SSH access to the controller, forward its desktop:

```bash
ssh -N -L 18181:127.0.0.1:18181 agent@192.168.0.13
```

Then open `https://localhost:18181`. This requires an existing user SSH identity
that permits forwarding. The build-worker management key is explicitly restricted
and does **not** permit port forwarding; do not relax that restriction. Automated
control on the build worker uses ordinary SSH command execution through the
controller, which was verified. Browser access to a build-worker loopback GUI
would require an independently authorized forwarding identity or the VM console;
that interactive browser path has not been configured. Keep the GUI private;
do not introduce public tunnels.

Automated interaction does not require a browser connection: the existing
Actions-to-controller-to-SSH path can call the bounded GUI commands and collect
screenshots. Direct interactive browser control from this ChatGPT environment
still requires a reachable authenticated browser endpoint; private LAN addresses
are not reachable from this execution environment. The scripted screenshot and
input path works independently of that limitation once validated on the workers.

## Worker acceptance evidence (October 22, 2026)

- Both existing workers successfully launched the pinned LinuxServer Calibre
  image and reported Calibre 9.16 with bundled Python 3.14.8.
- [GUI acceptance run](https://github.com/jfroebe/chatgpt-dev-control/actions/runs/39849014743)
  passed on both workers. Each typed a unique marker, clicked a real Qt button,
  verified the resulting value, sent a key and saved a valid PNG screenshot.
  The screenshots and receipts are retained as Actions artifacts. Both desktops
  and their temporary password files were removed after the test.
- The active runner on `chatgpt-build-02` is repository-scoped to
  `metadata-engine-docker`. Companion Docker PR 56 adds the targeted runtime
  workflow there, using the existing repository token for the exact private
  engine checkout. It requires no additional runner registration.

The earlier host-native launcher limitation no longer blocks container testing.
The older matrix rows describe the initial assessment; container and GUI
acceptance above supersede their unverified state for those specific checks.
Full release qualification remains distinct from container/GUI acceptance.

## Passed actual-engine and library run

[Build-worker run 39850526224](https://github.com/jfroebe/metadata-engine-docker/actions/runs/39850526224)
passed the complete implemented Linux container harness. It installed the prior
ZIP and candidate, registered and loaded the plugin, passed ten regressions,
retrieved The Hobbit metadata and cover through the actual pinned engine,
verified repeat queries skipped provider execution, wrote metadata and cover to
a synthetic disposable Calibre library, queried the expected ISBN/title/source,
removed the plugin, and reinstalled it. No production or personal library was
mounted. The test container was removed.

The tested client revision is
`1696c3b33ded48d8634fa36ed27902fc4bb312dd`; the exact ZIP SHA-256 is
`9e4c1a449f429cd9bd85ccecbeed9d16269631411c936127d5b862510516141f`.
The actual engine is pinned to
`22cc84260a479461ad90a11d5485d0591f65779d`.

- [Sanitized runtime reports](https://github.com/jfroebe/metadata-engine-docker/actions/runs/39850526224/artifacts/12457217094)
- [Exact tested ZIP artifact](https://github.com/jfroebe/metadata-engine-docker/actions/runs/39850526224/artifacts/12456343713)
- [GUI screenshots and input receipts](https://github.com/jfroebe/chatgpt-dev-control/actions/runs/39849014743/artifacts/12455528749)

Container/GUI implementation is verified. Full release qualification remains a
draft because the other explicitly listed release blockers are still open.
The container/GUI infrastructure was merged. No release was published or deployed.

## Native qualification evidence (October 23, 2026)

[Run 39953556989](https://github.com/jfroebe/calibre-plugin-metadata-engine/actions/runs/39953556989)
passed packaging, all ten installed-plugin regressions on each operating system,
preference preservation across installation of the previous and candidate ZIPs,
removal/reinstallation, and the final CI aggregation job. Windows Server 2025
and Ubuntu 24.04 both reported **Calibre 9.15 with bundled Python 3.14.7**,
covering the runtime generation in the reported Windows failure.

Both native jobs consumed the same candidate artifact and reported ZIP SHA-256
`9e4c1a449f429cd9bd85ccecbeed9d16269631411c936127d5b862510516141f`.
This also matches the previously verified Linux container/actual-engine artifact.
The native jobs do not themselves run the private engine or real authenticated
relays. Their success must not be described as those integration checks passing.

The first Windows attempt exposed a host-harness encoding error while reading
Calibre's Unicode output. Reading UTF-8 explicitly fixed it without changing the
plugin or dropping assertions. A host subprocess regression covers that behavior.
