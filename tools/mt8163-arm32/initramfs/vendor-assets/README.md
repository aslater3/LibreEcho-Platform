# MT8163 owner-local vendor assets

The firmware bytes are **not distributed** by LibreEcho. The image contains only
an importer, expected source paths, byte counts, and SHA-256 identities.

The firmware is treated as proprietary owner-device-local data. It is copied at
runtime only from the device owner's read-only system_a partition after the
partition identity, file type, size, and SHA-256 values have been checked. The
stock partition is mounted read-only with `nosuid,nodev,noexec`. Files are
verified in a mode-`0700` transient directory under `/tmp`, installed into
`/lib/firmware` with mode `0600`, and the transient copies are removed before
the importer exits. Vendor bytes are not persisted under `/data`, preserving
compatibility with an older confirmed rollback image's userdata allowlist.

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
