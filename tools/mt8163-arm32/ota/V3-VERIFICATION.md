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
checks passed. A second rerun exposed one further stale rollback-channel
source assertion after removal of unused version/channel variables; it was
replaced with schema-3 transaction identity assertions, retaining the channel
signature gate. The final fresh rerun below passed.

The mandated command remains the controller's `run-platform-ci.py` invocation
with Python 3.11, bundled host dependencies, pinned UI source and sleep header.
BusyBox `sh -n` applies to modified BusyBox scripts and adapted companion scripts;
`bash -n` applies to the Bash bundle builder. `git diff --check` is required.
Network archive retrieval, real ARM packaging, physical service acceptance,
hosted CI and any push/PR/release remain explicitly not observed.

### Observed final local PASS

- Tested code/test head: `65eac99956ae01787fca167de6900fe282febd3f`.
- Controller command executed exactly as supplied; exit status 0.
- 17 command groups; no nonzero command.
- 913 unittest executions; 0 failures; 0 errors; 22 explicit skips.
- Log: `platform-v3-fix.log` alongside the worktree.
- Log SHA-256: `8dac21c1c5ed5e54fcff979a8893ef1ba0789c5d0d1d7d841f907d1ef040fb5f`.
- Modified BusyBox scripts passed `busybox sh -n`; Bash bundle builder passed
  `bash -n`; adapted real companion init scripts passed BusyBox syntax inside
  the integration test. `git diff 71b30235..HEAD --check` passed.
- This record is a documentation-only follow-up to the tested head; no code or
  test changed after this successful aggregate execution.
