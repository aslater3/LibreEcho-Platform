# LibreEcho MT8163 OTA contract

## Platform mapping

The Biscuit Amonet layout has two layers of boot storage:

| BCB slot | Amonet entry/wrapper | Redirected OS image |
| --- | --- | --- |
| A | `boot_a`, `/dev/mmcblk0p17`, 225280 sectors | `boot_a_x`, `/dev/mmcblk0p10`, 32768 sectors |
| B | `boot_b`, `/dev/mmcblk0p18`, 225280 sectors | `boot_b_x`, `/dev/mmcblk0p11`, 32768 sectors |

Amonet hooks LK reads of `boot_a` and `boot_b` and redirects the Android boot
image reads to the corresponding `_x` partition. It also redirects normal
fastboot `flash boot_a`/`flash boot_b` commands to `_x`. Linux does not pass
through that fastboot hook, so the on-device updater must write `_x` directly.
The large `boot_a` and `boot_b` partitions contain the Amonet header and tail
payload and are read-only invariants for OTA.

The Amazon BCB is 7 bytes at offset `0x360` in `misc`
(`/dev/mmcblk0p8`, 1025 sectors):

```text
zero, "ABB", version,
A(priority:4, tries:3, successful:1),
B(priority:4, tries:3, successful:1)
```

The preloader selects the highest-priority slot that is successful or still
has tries. Before activating an update, the running slot is recorded as
`priority=14, tries=0, successful=1`; the target becomes
`priority=15, tries=3, successful=0`. A healthy target is confirmed as
`priority=15, tries=0, successful=1`. If all target attempts fail, the
preloader falls back to the successful priority-14 slot.

## Write allowlist

An ordinary OTA transaction may write only:

1. the inactive redirected image store, `boot_a_x` or `boot_b_x`;
2. the single 512-byte `misc` sector containing the BCB; and
3. `/data/libreecho/update`, which holds downloaded packages and transaction
   state on the existing `userdata` filesystem.

It must never write or format `boot_a`, `boot_b`, `persist`, `userdata`,
`lk_a`, `lk_b`, either TEE partition, recovery, cache, GPT, RPMB, or
the eMMC boot areas. User configuration remains under
`/data/libreecho/config`; IDME, calibration, MAC addresses, and identity remain
in `persist` and the eMMC boot areas.

There is one bounded migration exception for an installation launched from an
old development image. If, and only if, `expdb` has its exact expected
partition identity and begins with `FASTBOOT_PLEASE`, the installer preserves
the first sector, clears those 15 marker bytes, syncs, and verifies the complete
sector. No other `expdb` content is changed. OTA-profile images do not write
the marker again.

## Signed bundle v1

The transport is a deterministic POSIX tar with these exact members:

```text
manifest
manifest.sig
boot.img
```

`manifest.sig` is a lowercase hexadecimal Ed25519 signature over the exact
manifest bytes. The release public key is embedded in the boot image. The
manifest identifies the board, SoC, ARM architecture, version, boot image size,
SHA-256 digest, and required `image_profile=ota`. Version 1 deliberately updates the boot image only;
persistent feature squashfs payloads and all configuration remain unchanged.

Manual browser upload streams the tar to `/data/libreecho/update/incoming` and
invokes the target installer. OTA-profile images also check the stable public
GitHub Release asset configured in `/etc/libreecho/ota-source.conf`. The
fetcher mounts the root-owned assistant feature payload read-only, verifies it
against its staged manifest, and uses its pinned curl and CA bundle. It then
passes the downloaded tar to the same installer; it never implements a second
flashing path. The background watcher checks for a release but does not install
it by default. Users can install an available release explicitly in the system
UI or opt in to automatic installation; automatic installation never forces an
unattended reboot. The opt-in is stored as
`/data/libreecho/update/automatic-updates` and is disabled when that file is
absent. No transport is allowed to bypass
signature, member-list, image-format, partition-identity, inactive-slot,
copy-hash, or BCB readback checks.

Release builds run through the **Build and release LibreEcho OTA** GitHub
Actions workflow. A GitHub-hosted job first runs the complete Kernel recovery
contract and LibreEcho-UI test suites. A runner carrying the
`libreecho-ota-builder` label then uses the locally provisioned, hash-pinned
pipeline inputs to build the exact selected Kernel and UI commits, runs the
canonical independent image verifier, and uploads only the verified 16 MiB
image and provenance.

Signing remains isolated in the `production-signing` environment. That job
revalidates the artifact digest, manifest, source identities, and public-key
pin before environment approval exposes `OTA_SIGNING_KEY_HEX`. It constructs
the deterministic tar, independently verifies its exact members and Ed25519
signature, and uploads the image, manifest, verification log, provenance,
signed stable asset, and checksum to a draft release. Publishing is a separate
step that runs only after a push to protected `main` passes every preceding
job and receives production-signing approval. Branch pushes run source checks
only. The repository variable `LIBREECHO_PIPELINE_ROOT` must point at the
canonical pipeline on the labeled builder. The repository secret
`UI_REPOSITORY_TOKEN` must be a fine-grained token with read-only Contents
access to the private `aslater3/LibreEcho-UI` repository. The build always
forces GitHub signing mode and never reads a local signing key; only the
protected hosted signing job receives `OTA_SIGNING_KEY_HEX`.

The self-hosted build and protected release jobs are disabled unless the
repository variable `ENABLE_SELF_HOSTED_OTA` is exactly `true`. Until a
dedicated runner is provisioned, Kernel and UI branch checks run independently
in their respective private repositories and main merges do not queue a build.

## Development marker

The current development image writes `FASTBOOT_PLEASE` to `expdb` and resets
the BCB to seven tries on every boot. Both behaviors defeat OTA selection and
rollback. They remain available only in an explicit development build profile.
A release/OTA image does not write `expdb` automatically and does not reset the
BCB. An explicit operator request to reboot to fastboot may still write the
marker after validating the `expdb` identity.
