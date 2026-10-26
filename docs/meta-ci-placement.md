# Meta test execution

All Calibre build, style, package, evidence and release jobs execute on the existing Meta test workers. GitHub provides repositories and Actions coordination; GitHub-hosted runners are not required.

- `chatgpt-dev`: dedicated repository runner labeled `calibre-controller,chatgpt-dev`; packaging, checks, evidence and exact-artifact distribution.
- `chatgpt-build-02`: dedicated repository runner labeled `docker-build,chatgpt-build-02`; native official Linux Calibre and upgrade/lifecycle checks.
- Docker repository qualification: its existing controller and build runners also verify SQLite/PostgreSQL persistence, authenticated proxies and the offline pinned Calibre container.

Windows 11 desktop and macOS desktop retain the user's independent validation and are outside this test environment. No automatic Windows result is claimed. Linux native and container, package, engine/proxy and exact-ZIP evidence remain mandatory.

Repository-scoped runner registration must be completed before these Calibre workflows can execute. Worker host/account guards and the local Docker socket requirement are mandatory. Use isolated Python tool environments. Keep test database copies private and source SMB mounts read-only; never upload metadata.db as a CI artifact.
