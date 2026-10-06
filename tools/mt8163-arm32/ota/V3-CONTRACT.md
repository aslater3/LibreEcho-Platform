# LibreEcho direct-userdata install — protocol 2 / OTA target schema 3

Status: **design + recovery-side implementation**. This file is the authoritative
interface the browser/installer parent must consume. Written before code; any
implementation that disagrees with this file is a bug in one of the two.

## 1. What changes and why

The legacy recovery install stages ~240 MB of payloads **on `/cache`**, then the
installer formats userdata and copies the payloads onto it. `/cache` is under a
gigabyte and holds the previous bundle, so the push is fragile and the payload
set is duplicated (cache + userdata).

The v2 protocol replaces that with:

```text
host holds the source  →  prepare layout  →  initialize userdata (format once)
  →  transfer payloads straight onto the *formatted userdata*  →  finalize
     (write boot slots + place features) — finalize can never format
```

There is **no payload in `/cache`**. The userdata filesystem itself is the
landing zone, so nothing is duplicated; finalize links the uploaded files into
their final feature paths **in place** (same-filesystem hardlink), it does not
copy them.

The legacy `/cache` path (`twrp install …`) stays valid for existing hosts and
is left byte-compatible. The two share one bundle; v2 is selected **only** by an
explicit `--protocol 2` argument.

## 2. Files and where they live

### On the host (browser)

| File | Meaning |
|---|---|
| `libreecho-<slug>-install.zip` | the installer zip, verified by digest |
| `libreecho-<slug>-bundle.manifest` | key=value pin of every payload (the anchor) |
| `bundle.json`, `SHA256SUMS` | machine-readable summary / digests |
| payload files | boot image, feature payloads + manifests, signed `manifest` + `manifest.sig`, local-install tar |

The browser **extracts `libreecho-direct-install.sh` from the verified zip on the
host** (TWRP's toybox `unzip` on the device ignores patterns and extracts
nothing) and pushes that one self-contained file to the control-plane dir.

### On the device — control plane, `/cache` (small, no payloads)

```text
/cache/libreecho-direct/
    libreecho-direct-install.sh   the self-contained helper (pushed by the browser)
    bundle.manifest               the anchor manifest (pushed by the browser)
    install-manifest.json         the release install manifest (optional metadata; the helper does not require it)
    transaction.state             the persisted transaction guard (§7)
    receipt                       key=value result of the last phase (§6)
    install.log                   human-readable log
```

Nothing else is written to `/cache`. The legacy `/cache/libreecho-bundle/…`
content is **never cleaned up** by this protocol.

### On the device — bulk, `/data` (after the format)

```text
/data/libreecho/incoming/<name>               verified transfer landing zone
/data/libreecho/generations/<transaction_id>/
    target.manifest                         signed complete target schema 3
    target.manifest.sig
    features/<f>/payload.squashfs            exact target SHA-256
    features/<f>/manifest.json               exact target SHA-256
    COMPLETE                                target manifest SHA-256, written last
/data/libreecho/update/current               atomic pointer to installed generation
/data/libreecho/update/previous              prior generation (OTA rollback)
/data/libreecho/update/pending               schema 3 slot / transaction / manifest digest
/data/libreecho/config/                      only carried-over, schema-versioned user data
```

`incoming/` entries are **hardlinked** into the final paths (same filesystem), so
the payload exists once on userdata. `incoming/` is retained as the retry/evidence
source; it costs no extra blocks because it shares inodes.

## 3. Phases and required ordering

| # | phase | mutates | notes |
|---|---|---|---|
| 1 | `prepare` | userdata **GPT entry** only | reshapes userdata to a contract size; may need a reboot |
| 2 | `initialize` | userdata **filesystem** | formats **exactly once** (the sole format path), mounts, verifies the mount is real userdata, creates the tree |
| 3 | `transfer` | creates `incoming/` only | free-space gate; the browser then pushes payloads into `incoming/` |
| 4 | `finalize` | boot_a, boot_b, feature tree | verifies every upload, writes both boot slots, links features. **No format is reachable.** |

The "did everything land?" gate is `finalize --dry-run`: it runs every check
including the per-upload digest verification and writes nothing. The browser
runs it after the pushes and only then runs the real `finalize`. `transfer` is
called *before* the pushes, so it cannot verify files that are not there yet.

Ordering rules:

* `prepare` must run before `initialize`; `initialize` before `transfer`;
  `transfer` before `finalize`.
* If `prepare` reshaped the partition it reports `reboot_required=1`; the
  **browser** performs the reboot (recovery re-reads the GPT) and then runs
  `initialize`. The helper never reboots.
* A reboot between `initialize` and `transfer` is allowed but `/data` must be
  remounted; `transfer`/`finalize` remount if needed.

## 4. Invocation

The browser pushes the helper, then runs it via `/sbin/sh` with **explicit**
arguments (there is no implicit TWRP phase marker in v2):

```bash
adb push libreecho-direct-install.sh /cache/libreecho-direct/libreecho-direct-install.sh
adb push bundle.manifest             /cache/libreecho-direct/bundle.manifest
adb shell /sbin/sh /cache/libreecho-direct/libreecho-direct-install.sh \
    --protocol 2 \
    --phase <prepare|initialize|transfer|finalize> \
    --bundle-manifest /cache/libreecho-direct/bundle.manifest \
    --bundle-manifest-sha256 <hex64> \
    --target <radar_puffin|biscuit> \
    --release <release-id> \
    [--state-dir /cache/libreecho-direct] \
    [--incoming-dir /data/libreecho/incoming] \
    [--dry-run] \
    [--reset-transaction]        # only honoured with --phase prepare
```

Argument rules:

* `--protocol 2` is **required**. A missing `--protocol` is `result=failed
  error=protocol-required`; any value other than `2` is
  `error=unsupported-protocol:<n>`. The helper fails closed.
* `--bundle-manifest-sha256` must equal the sha256 of the `--bundle-manifest`
  file; a mismatch is `error=bundle-manifest-digest-mismatch` before any write.
* `--target`/`--release`, when given, must match `bundle.manifest`; mismatch is
  `error=target-mismatch` / `error=release-mismatch`.
* `--dry-run` runs every check and writes nothing; the receipt says
  `result=dry-run-ok`.
* Unknown arguments are `error=unknown-argument:<arg>`.

## 5. `bundle.manifest` fields (v2 additions are additive)

Existing keys (unchanged): `schema`, `release`, `device`, `target`,
`fastboot_products`, `soc`, `image_profile`, `service_profile`,
`userdata_sectors`, `install_manifest=<name>:<sha>`, `boot_image=<name>`,
`boot_image_sha256=<sha>`, `payload=<name>:<sha>`, `staging=<feature>:<payload>:<payload-sha>:<manifest>:<manifest-sha>`,
`local_package=<name>:<sha>`.

**v2 adds:**

| field | meaning |
|---|---|
| `protocol=2` | selects v2; its presence + `--protocol 2` must agree |
| `transfer_bytes_total=<int>` | sum of bytes the browser must transfer to userdata |
| `transfer=boot:<name>:<sha256>` | boot image role |
| `transfer=ota-manifest:<name>:<sha256>` | signed `manifest` role |
| `transfer=ota-signature:<name>:<sha256>` | `manifest.sig` role |
| `transfer=local-package:<name>:<sha256>` | OTA tar role |

Features come from `staging=<feature>:<payload-name>:<payload-sha>:<manifest-name>:<manifest-sha>`.
The helper locates each uploaded file in `incoming/` **by digest**, never by
name, so the release's published name and the signed manifest's asset name may
differ; the `transfer=` lines carry the fixed roles, and every feature is required by the **signed v3 target manifest**. No per-feature
action, base hash, preserve instruction or runtime overlay is accepted.

## 6. Receipt keys (`$STATE_DIR/receipt`, key=value, reset each run)

Common: `protocol`, `phase`, `result`, `error`, `target`, `release`,
`bundle_manifest_sha256`, `device_digest`, `userdata_sectors`, `format_state`.

| phase | result values | extra keys |
|---|---|---|
| `prepare` | `prepare-ok` (reshaped), `prepare-noop` (already in contract) | `reboot_required=0\|1`, `userdata_sectors` |
| `initialize` | `initialized` | `format_state=formatted`, `data_mount=<node>` |
| `transfer` | `transferred` | `transfer_bytes_total=<n>`, `incoming_dir`, `free_bytes` |
| `finalize` | `installed` | `boot_a_sha256`, `boot_b_sha256`, `hardlinked=<n>`, `moved=<n>`, `features=<csv>`, `local_package` |
| any | `dry-run-ok`, `failed` | `error=<token>` on failure |

`finalize --dry-run` therefore receipts `result=dry-run-ok` and is the landed-
completely gate; a missing/corrupt/truncated/symlinked upload fails it (and the
real `finalize`) with `error=missing-upload:<role>`.

The **receipt is the result** — the helper's exit status is not. `twrp install`
returns 0 for the legacy path even when it aborts, and `adb shell` status alone
is likewise not the outcome; read `receipt.result`.

## 7. Transaction guard (`$STATE_DIR/transaction.state`, key=value)

Persisted **before the first mutation** of `initialize`, and updated on every
phase. Binds the run to one release and one device:

```text
protocol=2
target=<id>
release=<id>
bundle_manifest_sha256=<hex64>
device_digest=<hex64>          # sha256(target+serial+userdata-guid); the raw serial is never stored
format_state=absent|formatting|formatted
phase=<last completed phase>
```

Fail-closed rules (all `result=failed`):

* `format_state=formatted` + another `initialize` → `error=already-initialized`
  (**never reformats**).
* `format_state=formatting` (crashed mid-format) + `initialize` →
  `error=format-uncertain` (operator must decide; the helper never silently retries).
* guard `bundle_manifest_sha256`/`target`/`device_digest` ≠ current →
  `error=transaction-conflict:<key>`.
* `phase=finalized` + `finalize` again → `error=already-finalized`.
* A phase run out of order (e.g. `finalize` with `format_state != formatted`) →
  `error=phase-order:<detail>`.
* Only `--reset-transaction` **with `--phase prepare`** removes the guard; it is
  the single explicit "start a genuinely new install" action and is receipted as
  `transaction_reset=1`.

## 8. Legacy coexistence

* `twrp install /cache/libreecho-bundle/libreecho-install.zip` keeps working
  unchanged; the legacy `update-binary` and its `/cache` staging are untouched
  apart from the new guard below.
* The legacy path now **refuses an install whose bundle directory is on the same
  block device as userdata** (`error=bundle-on-userdata`): the legacy flow
  formats userdata, so a bundle sourced from it destroys itself mid-run.
* v2 never writes a phase-marker file; `--phase` and `--dry-run` are explicit.
  The legacy `/cache/libreecho-install-dry-run` flag file is legacy-only.

## 9. Deliberately preserved safety gates

Target + product identity; userdata layout fingerprint (sectors must be one of
`2137088 2153472`); explicit write-set (`boot_a`, `boot_b`, `userdata`) with
`expdb`/LK/TEE/preloader/GPT never written; partition identity resolved by
`PARTNAME` **and** sector count (a same-size wrong node is refused); mount
verification against the real userdata node, never a ramfs; digest readback of
every boot-slot write; digest verification of every uploaded file before any
boot write; signature verification remains the booted OS updater's job (no
cryptographic verifier is shipped in TWRP — the helper does hash pinning only).

## 10. Out of scope (parent's job)

WebUSB/fastboot phases, download + host-side digest verification, computing
`--bundle-manifest-sha256`, the reboot after `prepare`, and rendering
`receipt.result`/`receipt.error` to the operator.

## OTA v3 generation authority

Recovery transfer protocol 2 is a browser/recovery phase protocol, not OTA format
v2. New target-state releases use `format=libreecho-ota-v3` and
`minimum_updater_schema=3`; v1/v2 OTA packages fail `legacy_manifest_unsupported`.
The finalized fresh-install generation is byte-identical to OTA assembly for the
same signed release. It becomes immutable (directories 0500, files 0400) only
after the exact file set and hashes verify, then `current` is atomically written.
Only versioned `config/` carries over. Old `features/`, runtime overlays and
installed-record histories are not payload authorities. Existing legacy devices
enter v3 through a recovery reinstall, never a composed OTA bridge.

OTA keeps `current`, `previous`, and `pending` generations; garbage collection
removes other directories whole. Mutations share `update/generation.lock` with
an owner PID and kernel boot ID, atomically renamed from an owned temporary
directory. This remains on userdata so boot and recovery callers share scope.
Live owners and nonempty ambiguous locks fail closed; empty legacy ownerless
locks, dead owners or a changed boot ID permit serialized reclamation. A crash
before publication leaves a harmless unpublished temporary directory. A
SIGKILL between durable commit writes is resumed without changing the previous
rollback target. `installed` and `rolled-back` are schema-3 diagnostic summaries,
not authorities for feature bytes.

Boot target failures leave the web/update control plane running, expose
`features_state` and `features_error`, and stop feature daemons. Unknown config
schemas leave user bytes untouched and expose `config_error`; the UI displays a
banner. Platform's build-time companion UI adapter applies these fields to a
private source snapshot and records its digest; it never edits a device or the
caller's UI checkout. Host C/JS fixtures verify the status serializer and banner;
these are not hardware or hosted-CI qualification.

### Repair transport limitation

The HTTPS mount verifies the current target manifest signature and only the
assistant payload and feature manifest size/digest pins. Corrupt STT or another
unrelated feature does not prevent fetching a repair. A corrupt assistant fails
`https-transport-corrupt`; fetch uses `/usr/bin/curl` plus
`/etc/ssl/certs/ca-certificates.crt` only if both exist as boot-resident regular
files. Otherwise it fails `https_transport_unavailable`. Images without this
optional boot client require recovery/local install to repair the transport;
no new bootstrap client is implied.

### Downgrade and replay policy

Operator-authorized downgrade/reinstall is permitted only through explicit
`libreecho-update install PACKAGE` or `libreecho-update-fetch install`. Signature,
target, and whole-generation checks still apply; there is no monotonic sequence.
`check` and watcher-only `auto-install` suppress a candidate whose signed
`release` equals the authenticated current or previous target's release, or whose
`transaction_id` equals the retained schema-3 `rolled-back` marker. Rollback
suppression survives deletion of its generation; it is not conditioned on
feature health. The marker retains the most recent rollback, not an unbounded
history of every rejected release. There is no claim of global anti-replay or
ordering among other signed releases. Explicit install bypasses these discovery
suppression rules, never signature verification.

### Generation lifecycle and companion packaging

Assembly publishes `generations/<transaction_id>.pin` under `generation.lock`.
GC snapshots pins before current/previous/pending and preserves every pinned
target. The installer clears its pin only after durable pending publication or
an ordinary abort; SIGKILL retains it for recovery/operator cleanup. Commit and
rollback invoke GC while still holding the generation lock. Standalone GC
refuses an active install lock. GC removes only whole obsolete generation trees.

Tests select the sparse pinned companion through `LIBREECHO_OTA_UI_SOURCE`;
CI pins LibreEcho-UI commit `17604803f4682826f3fdb6f0af0c52bb064dd853`.
That sparse checkout originally lacked `init/`; captured real review scripts are
checked in as host regression fixtures when absent. Production gets its full UI
checkout as `build_ui_bundle.sh` argument 1 or `LIBREECHO_UI_SRC`, adapts a private
snapshot, and guards the actual packaged scripts against legacy feature paths.
Feature init scripts consume Platform-owned read-only generation mounts and do
not unmount their roots. Upstream UI should adopt this ownership model eventually;
no upstream change is required for the Platform build-time adapter.
