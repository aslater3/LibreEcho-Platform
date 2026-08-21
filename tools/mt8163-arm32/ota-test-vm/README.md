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
- **PHASE1 — signed `package.tar`** → `ota_manifest_signature=PASS` →
  `UPDATE_READY slot=b`, BCB flipped to the inactive slot.
- **PHASE2 — unsigned `unsigned.tar`, no flag** → rejected with
  `ERROR:package_signature`.
- **PHASE3 — `unsigned.tar --allow-unsigned`** → verification skipped →
  `UPDATE_READY`.

The BCB is reset to a fresh, current-slot-confirmed state before each mutating
install so the phases run in one boot without tripping the A/B safety gate.

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
sed -n '/===PHASE0/,/===PHASE3_END/p' boot-test.log
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
