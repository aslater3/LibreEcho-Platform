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

## Independent review correction gate

Local fix commits address process executable identity, measured empty suffixes,
transport-only authentication, atomic lock publication, durable candidate pins
and GC, committed boot hash binding, packaged companion init ownership,
automatic replay suppression, and per-target development pointers. The mapping
names the focused real-helper regressions and their negative counterparts.

The first full correction rerun (`platform-v3-fix.log`) reported 912 tests,
5 failures, 0 errors, 22 skips. All five were obsolete source-shape assertions:
radar-only URL twice, old rollback-version guard, mkdir-first lock, and
whole-generation HTTPS verification. They were updated to check the corrected
contracts without removing refusal gates; all six focused replacement/crash
checks passed. A fresh complete rerun is required before a PASS claim.

The mandated command remains the controller's `run-platform-ci.py` invocation
with Python 3.11, bundled host dependencies, pinned UI source and sleep header.
BusyBox `sh -n` applies to modified BusyBox scripts and adapted companion scripts;
`bash -n` applies to the Bash bundle builder. `git diff --check` is required.
Network archive retrieval, real ARM packaging, physical service acceptance,
hosted CI and any push/PR/release remain explicitly not observed.
