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
before the importer exits. Vendor bytes are not persisted under `/data`.

## Approved stock revisions

Each tracked `mt8163-v181-stock-v*.tsv` file is one complete, independently
approved four-file firmware revision. The importer tries each manifest as an
atomic set. A hash or size from one revision is never combined with records
from another revision to manufacture a match.

A complete match to one of those shipped manifests is reported as
`verification=hash-pinned`.

## Previously unknown owner-local revisions

Hash pinning cannot identify a blob that LibreEcho has never seen. The setup
API may therefore schedule the mode-`0600` one-shot marker
`/data/libreecho/config/vendor-import-force-next-boot` with the exact payload
`force-unverified-owner-local-import-v1`.

On the next boot only, this permits an otherwise unknown revision when all four
expected regular files are present in one safe stock layout, the two ROM patch
headers and routes match the MT8163 WMT contract, and each file remains within
a defensive 16 MiB limit (64 KiB for the configuration). The marker is consumed
before selection and that first import is reported as
`verification=forced-unverified`, never as globally hash-verified.

After a successful forced import, LibreEcho atomically stores **only** the exact
four SHA-256 identities, sizes, source paths, and target names in
`/data/libreecho/config/vendor-assets.tsv` with mode `0600`. No firmware bytes
are copied to persistent userdata. On later boots the stock files must match
that device-local manifest exactly and are reported as
`verification=owner-local-enrolled`.

If any enrolled file changes, disappears, or no longer forms the same complete
set, normal boot fails closed with `VENDOR_IMPORT_ENROLLED_SET_MISMATCH`.
Replacing the enrolled revision requires the owner to explicitly schedule the
one-shot force marker again. A device-local enrolment does not make the hashes
globally trusted; a revision should only be promoted into a shipped
`mt8163-v181-stock-vN.tsv` manifest after separate project validation.

The userdata cleanup contract already tolerates unknown regular configuration
files across A/B rollback, so the enrolled manifest remains preserved when an
older slot is selected. Symlinks and unexpected directories remain hard
failures.

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
