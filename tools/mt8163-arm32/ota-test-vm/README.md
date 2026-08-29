# LibreEcho OTA test VM

A **full-system** QEMU ARM VM that tests the A/B **OTA install** flow end-to-end
against an **emulated MTK eMMC** — the slot/flash paths the
[container emulation](../emulation/) explicitly can't cover. Use it to validate
`libreecho-update` on a real block device: signed release packages, and the
opt-in `--allow-unsigned` side-load.

> **Recipe only.** No binaries, kernels, or release packages are committed. You
> supply the update tools and the OTA packages under `stage/` (see below).

## What it exercises

The boot self-test (`init`, built by `build-initramfs.sh`) runs four phases and
powers off:

- **PHASE0 — `libreecho-update capabilities`** → prints `allow-unsigned`; an
  unknown subcommand exits non-zero (the fail-safe the web UI uses to gate the
  unsigned-upload checkbox).
- **PHASE_DATA_CONTRACT** → runs the production `libreecho-data-cleanup` over the
  seeded `/data` and asserts its verdict: a `--scenario` brick shape must be
  rejected with `DATA_CLEANUP_CONTRACT_FAILED` (and the VM powers off — the
  scenario's whole point is the rejection), a clean userdata must pass with
  `DATA_CLEANUP_OK`. This is what makes the scenarios evidence rather than
  inert files.
- **PHASE_PROFILE** *(only when `--profile` seeded a non-default BCB)* → installs
  from the captured slot before the reset below, asserting the install targets
  the inactive slot and leaves the captured slot as a confirmed-successful
  rollback. This is the only phase that covers an install from slot b or with a
  rollback available.
- **PHASE1 — signed `package.tar`** → `ota_manifest_signature=PASS` →
  `UPDATE_READY slot=b`, BCB flipped to the inactive slot.
- **PHASE2 — unsigned `unsigned.tar`, no flag** → rejected with
  `ERROR:package_signature`.
- **PHASE3 — `unsigned.tar --allow-unsigned`** → verification skipped →
  `UPDATE_READY`.

The BCB is reset to a fresh, current-slot-confirmed state before each mutating
install so the phases run in one boot without tripping the A/B safety gate.

## Seeding from a captured device (optional)

With no arguments `mkdisk.sh` produces exactly the image it always did. Two
optional arguments make the disk resemble a real device, or a broken one.

```sh
sh mkdisk.sh --profile device-profile.json
sh mkdisk.sh --profile device-profile.json --scenario config-dir
sh mkdisk.sh --scenario stray-data-file
```

`--profile` takes a captured device profile (produced by LibreEcho-UI's
`tools/capture_device_profile.py`, which redacts identifying fields). Its
`system_update` block decides which slot the BCB marks current and whether the
other is a genuinely bootable rollback; its `config_export` block is written to
`/data/libreecho/config/web-config.json` inside `userdata` -- the path
`libreecho-init` and the web service actually read -- so the VM boots with a
realistic configuration instead of an empty filesystem. A profile whose
`config_export` is missing or malformed is rejected rather than silently
seeding an empty configuration.

`--scenario` seeds `/data` into a shape known to break a real device:

| Scenario | Shape | Why it matters |
|---|---|---|
| `config-dir` | a **directory** where a config **file** belongs | halts every service on the next boot |
| `stray-data-file` | an unallowlisted file directly under `/data` | has left **both** A/B slots unbootable |

Both have bricked hardware. Rollback does not rescue either, because `/data` is
shared between slots — which is exactly why reproducing them in QEMU is worth
the trouble.

Verify the script with `vmtest.sh`, in the same privileged container the header
describes: it checks that the default image is unchanged, that the BCB follows
the profile, that both scenarios land in `userdata`, and that an unknown
scenario is refused.

## Prerequisites

- Docker with `linux/amd64` (privileged) **and** `linux/arm/v7` emulation
  (e.g. via `binfmt`/QEMU).
- `qemu-system-arm` on the host (the VM boots on the host, not in a container).

## Populate `stage/`

```
stage/
  tools/
    libreecho-update            # from ../initramfs/
    libreecho-bootctl           # build static: gcc -static ../ota/libreecho_bootctl.c
    libreecho-update-verify     # from ../ota/ (or ../initramfs libexec)
  ota-public-key.hex            # the verify key matching your signed package
  package.tar                   # a SIGNED release OTA tar (PHASE1)
  unsigned.tar                  # optional: a self-signed/unsigned tar (PHASE2/3)
```

Build a self-signed `unsigned.tar` for PHASE2/3 with
[`../ota/make_ota_bundle.py`](../ota/) using a locally generated ed25519 key, and
put the matching public key at `stage/ota-public-key.hex`.

## Run

```sh
D() { docker run --rm -i "$@" -v "$PWD":/work debian:bookworm-slim bash -s; }

D --privileged --platform linux/amd64 < mkdisk.sh          # fresh emmc.img
D            --platform linux/arm/v7  < build-initramfs.sh  # vmlinuz + initramfs.cpio.gz
./boot-test.sh                                              # boots QEMU -> boot-test.log
```

Then read the phase markers:

```sh
sed -n '/===PHASE0/,/===PHASE3_END/p' boot-test.log   # includes DATA_CONTRACT + PROFILE
```

## Notes

- Uses **virtio-mmio** (`-M virt`, `virtio-blk-device`), not QEMU SD emulation.
  The init symlinks `/dev/mmcblk0pN → /dev/vdaN` and shadows `/sys/class/block`
  so the tooling's hardcoded MTK partition identity checks pass; writes are real.
- **Keep `vmlinuz` and the initramfs modules the same version.** A stale kernel
  makes every virtio/ext4 module fail vermagic and no block device appears;
  `build-initramfs.sh` exports its own matching kernel to avoid this.
- The GPT layout, partition names/sizes, and BCB offset match what the tooling
  validates (`expdb`, `misc`, `boot_a_x/b_x`, `boot_a/b`, `userdata`).
