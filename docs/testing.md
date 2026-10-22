# Calibre integration testing

## Current assessment

**Draft implementation; not release-qualified.** No release is published or
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
assertions. These runtime tests are **written but not yet executed successfully**.

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

Required existing/secure credentials:

- `METADATA_ENGINE_REPO_TOKEN`, with Contents read access to the pinned component
  repositories. Git checkouts disable credential persistence. A missing token
  fails preflight immediately rather than leaving jobs queued.
- `CALIBRE_TEST_SOCKS_PASSWORD`, supplied securely for the existing authenticated
  relay. The implementation environment had no accessible value. Never commit
  it or put it in a workflow dispatch input.

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
| Calibre 9.15.0 official Linux x86-64 | Archive downloaded and ELF architecture verified; launcher fails before Python starts in the implementation environment |
| Calibre-bundled Python | Runtime version recording implemented; execution unverified |
| chatgpt-build-02 | Harness and workflow prepared; runner inventory/access still needed |
| chatgpt-dev | Actual engine tests prepared for worker; runner inventory/access still needed |
| Windows 11/current Calibre, Python 3.14-era regression | Not yet implemented or qualified; required before release |
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
- Verify current worker inventory and credential availability, then run and
  troubleshoot the actual Calibre harness. Do not mark its unexecuted assertions
  as passing or weaken them to obtain a green build.
- Implement and pass Windows qualification with pinned installer digest and the
  relevant bundled Python runtime. No permanent Windows VM is required.
- Extend lifecycle assertions to verify prior-version preference preservation
  and configuration migration, rather than merely installing old then new ZIPs.
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

The `qualification` job deliberately rejects this unqualified draft. Do not
merge it as a finished release gate. Existing distribution workflow remains
unchanged in this draft to avoid replacing successful distribution with an
unverified harness. Wiring it to consume the same tested ZIP is required before
qualification, and must be completed before a new release is published.

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
python ci/gui_control.py start --engine-root /path/to/pinned-engine \
  --report /path/to/private/gui-session --port 18181
python ci/gui_control.py windows --name <reported-container>
python ci/gui_control.py click --name <reported-container> --x 400 --y 300
python ci/gui_control.py type --name <reported-container> --text 'test query'
python ci/gui_control.py key --name <reported-container> --key Return
python ci/gui_control.py screenshot --name <reported-container>
python ci/gui_control.py stop --name <reported-container>
```

The GUI is a real X11 desktop (`PIXELFLUX_WAYLAND=false`) with xdotool and scrot.
The tool refuses to control containers lacking its ownership label. Browser
access uses Selkies HTTPS, bound **only to worker loopback**, with a generated
password outside artifact directories. `session.json` records the container,
port and password-file location but not the password. Do not upload that
password file. Remove it after ending a session.

From a machine with existing SSH access to the controller, forward its desktop:

```bash
ssh -N -L 18181:127.0.0.1:18181 agent@192.168.0.13
```

Then open `https://localhost:18181`. For the build worker, first forward its
loopback desktop to another controller loopback port using the existing pinned
host-key file and restricted management identity; forward that controller port
to the browser machine. The management key only accepts connections originating
from the controller. Keep the GUI private; do not introduce public tunnels.

Automated interaction does not require a browser connection: the existing
Actions-to-controller-to-SSH path can call the bounded GUI commands and collect
screenshots. Direct interactive browser control from this ChatGPT environment
still requires a reachable authenticated browser endpoint; private LAN addresses
are not reachable from this execution environment. The scripted screenshot and
input path works independently of that limitation once validated on the workers.
