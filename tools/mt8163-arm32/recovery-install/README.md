# LibreEcho initial-install (recovery installer)

Device-side initial installation for LibreEcho on the MT8163 Echo family, run
from TWRP. It is the second half of the fastboot-first install path: the host
orchestrator in the product repo finds and unlocks the device, and this package
puts the OS on it.

## Why this runs in TWRP and not on the host

Every operation this needs exists in recovery and **none** of them exist in the
installed OS or in a stock userland:

| Operation | TWRP 3.7 | Installed OS |
|---|---|---|
| Run an installer zip | `/sbin/twrp` → `install <zip>` | — |
| Reshape a partition entry | `/sbin/sgdisk` | — |
| Format ext4 | `/sbin/mke2fs` | — |
| Verify by hash readback | `/sbin/sha256sum` | absent |
| Partition lookup | `/dev/block/by-name/` | sysfs `PARTNAME` only |
| Reboot | supported | refused by adbd |

Doing it on the host instead would mean depending on whatever `sgdisk` and
`mke2fs` the user happens to have installed, and would put the only transaction
log on a filesystem we are about to overwrite.

Two recovery-side caveats worth knowing before writing anything that runs here:

* `unzip` exists but is **toybox's**. It ignores member patterns and `-d`, and
  extracts nothing while still exiting 0. Anything that must run in TWRP should
  carry what it needs in one file rather than unpack itself — which is why
  `update-binary` here is the whole installer and there is no `lib/` to load.
* There is no busybox (`/bin/busybox: not found`), so `/sbin/sh` (mksh) and the
  binaries in `/sbin` are all a script may rely on.

## What it writes, and what it must never write

Writable, and nothing else:

* `boot_a`, `boot_b` — the OS image, both slots
* `userdata` — its partition table entry (prepare phase) and its filesystem (install phase)

Never written: `expdb` (**this is where kaeru lives — the boot chain**),
`lk_a`, `lk_b`, `tee1`, `tee2`, `preloader`, the GPT, `persist`, `recovery`,
`system_a`, `system_b`.

`system_a` matters beyond safety: LibreEcho provisions the vendor firmware blobs
from it on first boot rather than redistributing them.

## Phases

The host drives both. Each run ends with a machine-readable receipt at
`/cache/libreecho-install-receipt`.

**prepare** — reshape `userdata` to a size the OS contract accepts.

The OS itself refuses to mount `/data` unless `userdata` is exactly
`2137088` or `2153472` sectors, and it rejects *larger* partitions on purpose,
so this is a layout fingerprint rather than an accident. Reshaping is needed
once per device and **requires a reboot**: the kernel keeps the old partition
table until it re-reads it, and a filesystem built against the stale size would
be larger than the partition holding it.

**install** — format `userdata`, write both boot slots, stage the features.

Staging happens *after* the format, and sources from the bundle, which lives
outside `userdata`.

## The trap that shapes the whole design

**In TWRP, `/sdcard` is `/data`.** They are the same partition:

```
/dev/block/mmcblk0p16 on /data    type ext4
/dev/block/mmcblk0p16 on /sdcard  type ext4
```

So a bundle pushed to `/sdcard` destroys itself during the format step. Push to
**`/cache`** (`mmcblk0p15`, 784 MiB, mounted in recovery) — or hold it in RAM,
where TWRP has ~432 MB free and the feature set is ~240 MB.

## Building a bundle locally

```bash
cd tools/mt8163-arm32/recovery-install

python3 build_install_bundle.py \
    --assets /path/to/release-assets \
    --out    /path/to/bundle \
    --release 0.14.0-dev-4a5859f \
    --device  mt8163
```

`--assets` takes either a release assets directory or a directory holding the
release's `*-initial-install.tar`, which is unpacked for you. The output is:

```
<bundle>/
    libreecho-install.zip     the installer zip TWRP runs
    bundle.manifest           shell-readable pin of every payload
    SHA256SUMS                digests for the whole bundle
    bundle.json               machine-readable summary
    <boot image>              the OS image
    <feature payloads>        *.squashfs and their *.manifest.json
    <ota-public-key.hex>
```

Payloads sit *beside* the zip rather than inside it: the feature set is ~240 MB
and TWRP's `/tmp` is a ramdisk. The manifest pins all of them either way.

The build is reproducible — same inputs, byte-identical zip — so a rebuilt
bundle can be compared rather than trusted. It also fails closed: a boot image
that is not exactly one slot (16 MiB), a missing payload, no features, a tar
with a traversal path, or a bundle that does not match its own manifest all
refuse to build.

Verify an existing bundle without rebuilding:

```bash
python3 build_install_bundle.py --assets . --out /path/to/bundle --release x --check
```

## Running it on a device

```bash
# bundle must be on /cache, not /sdcard
adb push libreecho-install.zip /cache/libreecho-install.zip
adb push <payloads>            /cache/
adb shell twrp install /cache/libreecho-install.zip     # phase: prepare or install
adb shell cat /cache/libreecho-install-receipt
```

If the device needed reshaping, the receipt says `reboot_required=1`; reboot
into recovery and run the same command again, and it takes the install path.

Rehearse without writing anything — every check runs, no device writes:

```bash
adb shell touch /cache/libreecho-install-dry-run     # the flag is how `twrp install` asks
adb shell twrp install /cache/libreecho-install.zip
adb shell cat /cache/libreecho-install-receipt      # result=dry-run-ok
adb shell rm /cache/libreecho-install-dry-run       # clear it, or the next run is a dry run too
```

The flag file exists because `twrp install` passes no extra arguments, so a dry
run cannot be requested on the command line when TWRP starts the installer.

## Producing it in CI

The same script is the single producer; CI adds provenance, not behaviour:

1. resolve the release (tag → assets), download and verify `SHA256SUMS`
2. run `build_install_bundle.py` against the released assets
3. sign the resulting `bundle.json` / `SHA256SUMS` with the release key
4. publish the bundle *separately* from the legacy installer inventory — older
   clients validate a fixed checksum inventory, and appending new assets to it
   breaks them

The installer zip and the compatibility metadata should be versioned
independently of the OS release, so an unqualified boot-chain change is never
attached to an OS version to make a number.

## Tests

```bash
python3 tests/test_build_install_bundle.py
```

Covers bundle structure, manifest pinning, reproducibility, and every
fail-closed path. **These are host tests.** They prove the bundle is
well-formed; they cannot prove the exploit, the boot chain or the installer's
behaviour on real hardware. That needs a device, a reboot, and a boot.

## Rehearsal status

Rehearsed on hardware (mt8163 Dot, TWRP 3.7.0_9-0) by dry-running the zip
through `twrp install`: partition resolution, verification of all 12 pinned
payloads, the userdata contract check, and the install phase's intended writes.
The device was byte-identical afterwards — `boot_a`/`boot_b` unchanged and
`expdb` still matching `biscuit-kaeru.bin`.

Not yet rehearsed through this package:

* the **prepare** phase. Its primitives were exercised by hand on the same unit
  (`sgdisk --delete --new` atomically, then a reboot to take the new table), but
  the package's own path has not run — that needs a unit whose userdata is
  outside the contract.
* a real, non-dry-run install end to end.
* anything on the radar profile.

Do not treat `twrp install`'s exit status as the result. It returns 0 even when
the installer aborts; TWRP prints `Updater process ended with ERROR: 1` and the
receipt at `/cache/libreecho-install-receipt` carries the real outcome and
reason.

## Not done yet

* Feature payloads are staged but nothing verifies they are *consistent with
  each other* (a mixed-generation set would stage silently).
* The prepare phase frees a ~220 MiB tail at the end of the disk and leaves it
  unused. If the intended layout puts something in that region — the 8 MiB gap
  between the two accepted sizes hints that it might — that is a second piece
  of work.
* No rollback path is offered: replacing the FireOS boot images ends the
  possibility of booting FireOS. Retained TWRP is the recovery route.
* Only the two accepted userdata sizes are handled. Any other layout stops with
  a diagnostic rather than guessing.
