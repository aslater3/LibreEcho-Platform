# OTA v3 local verification gate

Required: every maintained suite in `.github/workflows/ota-release.yml`, all v3
suites, recovery-install suites, offline Opus contracts, sleep arbitration with
the pinned companion header, Python compilation, tracked shell syntax, and
`git diff --check`. Compare the same offline workflow on detached
`origin/release/0.14.0`, with the v3 additions unavailable on that ref omitted.

The archive download/cross-build positive Opus steps require network and an ARM
prefix and are not credited by offline skips. Runtime acceptance, real ARM
packaging, hosted CI, physical devices, publishing, pushing and releases are
outside this gate. Local logs are stored alongside the worktrees as
`platform-v3-final.log` and `platform-base-final.log`.

New behavior followed red/green tests: stale lock reclamation, real SIGKILL at
commit durable boundaries, generation provenance, signed-current status, and
boot marker -> companion C status handler -> JavaScript banner integration.
Legacy coverage retirement and replacement are in `V3-TEST-MAPPING.md`.
