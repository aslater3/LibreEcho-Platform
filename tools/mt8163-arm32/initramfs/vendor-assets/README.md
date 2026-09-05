# MT8163 owner-local vendor assets

The firmware bytes are **not distributed** by LibreEcho. The image contains only
an importer, expected source paths, byte counts, and SHA-256 identities.

The firmware is treated as proprietary owner-device-local data. The importer mounts
the device owner's read-only system_a (`system_a`) partition with
`nosuid,nodev,noexec` and probes only these bounded Android layouts:

- `etc/firmware`
- `vendor/firmware`
- `system/vendor/firmware`
- `system/etc/firmware`

Every path component must be a real directory; symlinks are never followed.
Files are verified in a mode-`0700` transient directory under `/tmp`, installed
into `/lib/firmware` with mode `0600`, and the transient copies are removed
before the importer exits. Vendor bytes are not persisted under `/data`,
preserving compatibility with an older confirmed rollback image's userdata
allowlist.

Normal boot requires the exact size and SHA-256 contract in the TSV. The setup
API may schedule the mode-`0600` one-shot marker
`/data/libreecho/config/vendor-import-force-next-boot` with the exact payload
`force-unverified-owner-local-import-v1`. On the next boot only, this permits an
unknown hash/size revision when all four expected regular files are present in
one safe layout, the two ROM patch headers and routes match the MT8163 WMT
contract, and each file remains within a defensive 16 MiB limit (64 KiB for the
configuration). The marker is consumed before selection and the result is
reported as `forced-unverified`, never as hash-verified.

Machine-readable import state is published at
`/run/libreecho/vendor-import.status`. The Web setup flow combines that state
with live `/sys/class/net/wlan0` registration; a successful import alone is not
Wi-Fi readiness.

A compatibility hash does not grant redistribution rights. Do not add the
firmware, a stock filesystem, or an extracted stock boot image to this
repository, a release archive, CI artifacts, or public evidence. Device owners
and distributors remain responsible for confirming that their use of stock
firmware is permitted in their jurisdiction and under the terms applicable to
their device.

For the MT8163 WLAN driver, the importer creates the runtime regular-file alias
`/lib/firmware/WIFI_RAM_CODE` from the verified stock
`WIFI_RAM_CODE_8163`. The initramfs provides `/etc/firmware` as a relative link
to `../lib/firmware`, matching the driver's literal firmware path without
embedding or redistributing vendor bytes.
