# 0.14 qualification gates

This harness is a read-only hardware baseline plus an evidence aggregator. It is
not yet an automated implementation of every feature trial below. Missing trials
are HOLD, never inferred from source tests or a booting image. Operator-authored
records and hashes provide traceability, not independent certification.

## Required progression

1. Finish review corrections and merge to the intended release branches. Carry
   fixes made on integration PRs into the source set actually used by Product CI.
2. Freeze Product, Platform, Linux, and UI commit SHAs. Inventory every included
   commit from the previous accepted release and map it to the gates below;
   unmapped behavior changes block completion. Re-run source suites on these
   combined heads, not only isolated PR heads.
3. Run the disk-backed QEMU OTA matrix in its disposable privileged environment:
   clean first install; slot A/B; v1-to-v2; successful-v2 then failed-v2;
   interrupted prepare/commit; rollback; retained settings; malformed userdata;
   absent/present feature payloads. QEMU cannot validate MT8163 drivers.
4. Build the development candidate through Product CI, without stable release
   publication. Download and independently verify signatures, boot/payload
   digests, ARM ABI/runtime closure and all four resolved source identities.
5. Before installation, identify the exact physical device and confirmed rollback
   image, preserve operator configuration privately, verify recovery transport,
   and record a UART boundary. Installation, reboot and slot confirmation are
   separate reviewed operations; this harness does none of them.
6. Run baseline, feature trials, cold boots and soak against the exact dev artifact.
   Keep physical observations separate from daemon/API results. Preserve failed
   candidate evidence. Stop on lost transport, unexplained reboot, dead required
   daemon or rollback ambiguity; do not confirm the candidate to clear a test.

## Candidate identity input

A private JSON file has exactly `schema: 1`, `sources` containing the four exact
40-character commits (`product`, `platform`, `linux`, `ui`), `boot_sha256` and
`kernel_release` (the exact `uname -r` expected from the built kernel).
Populate these from independently verified build outputs, never placeholder SHAs.
Changing any field invalidates earlier candidate-bound records.

## Read-only baseline

From `tools/mt8163-arm32`:

```
python3 qualification.py baseline --candidate /private/candidate.json \
  --serial "$ADB_SERIAL" --samples 3 --interval 5 --output /private/baseline.json
```

This executes only fixed read-only commands through serial-bound `adb exec-out`.
It checks exact kernel release, advancing uptime, unchanged boot ID, an explicit
A/B slot and ALSA card registration. It does not read raw partitions, config,
passwords, microphone data, network addresses or serial logs. Raw cmdline and
serial identity are not saved. A timeout/failure stops the run. Output is exclusive
(no overwrite), mode 0600; pre-create a private parent directory.

## Feature trial matrix

| Gate | Required evidence |
|---|---|
| source-regressions | Exact combined-head Product/Platform/Linux/UI suites, API/OpenAPI and browser checks; included-commit coverage ledger |
| emulator-ota | Actual booted QEMU matrix above, negative paths and post-reboot state, not skipped unit tests |
| artifact-verification | Downloaded Product dev build, signatures, immutable source map, image/payload hashes and ABI/runtime closure |
| hardware-baseline | Harness output plus separately verified boot-image identity before promotion |
| setup-auth-persistence | Fresh bootstrap reachability, login/CSRF, add/change/delete users, normalized SSH/SCP accounts, HTTPS and settings after restart |
| voice-local | Live mic/wake/STT/TTS/agent processes, protocol responses, known utterance through speaker response; local vs external LLM declared |
| voice-home-assistant | Local/custom/HA transitions, settings-save rollback, silent wake, actual HA discovery/connect/audio round trip, no stale service after disable |
| mdns-without-airplay | HA advertisement with AirPlay payload absent; independent service recovery after network loss; correct port and removal after disable |
| audio-playback-capture | Low-level bounded playback and capture, routing/amp cleanup, physical acoustic observation, mute/privacy behavior |
| wake-barge-in | Detection during actual playback, interruption, no duplicate response, wake health restored |
| buttons-leds-clock | Physical button actions, LED priorities/cleanup, spoken time/timezone and timer behavior |
| radio-airplay-bluetooth | Real clients, decoded playback, metadata, stop/reconnect, competing audio owner transitions |
| ota-upgrade-rollback | Signed dev candidate inactive-slot install, post-reboot identity, failed candidate rollback, preserved config/features and subsequent update |
| cold-boot-persistence | Controlled cold and warm boot identity, configuration and service liveness; prior confirmed slot preserved |
| stability-soak | Declared duration/load, transport continuity, PID/socket/protocol liveness, resource trends, no panic/watchdog; recheck after settings saves |

`acoustic_events` is a preference placeholder, not a working detector. Verify its
persistence/API contract; do not report acoustic-event detection as tested.
Feature trial implementations remain to be added; this matrix makes that absence
visible rather than reporting blanket hardware readiness.

## Aggregation

Each external gate record contains `schema: 1`, `candidate_id` from the baseline,
`gate`, `status` (`PASS`, `FAIL`, `HOLD`, `SKIP`), and nonempty `artifacts` entries
with `path` and `sha256`. Only attest PASS after interpreting the actual trial's
acceptance criteria; artifact existence alone is not a test. Retain logs privately
and sanitize before sharing. The tool verifies artifact hashes, rejects duplicate
or foreign-candidate gates, and returns nonzero until all required gates PASS.

```
python3 qualification.py aggregate --candidate /private/candidate.json \
  --evidence /private/baseline.json --evidence /private/source-regressions.json \
  --output /private/qualification.json
```

This intentionally returns HOLD for that incomplete example. Never manufacture
records for unexecuted tests. Baseline probes do not authorize flashing or a slot
confirmation. No CI fixture result is physical hardware evidence.
