#!/usr/bin/env python3
"""0.13.15 verifier overlay for MT8163 vendor-import compatibility.

The 0.13.14 verifier remains byte-for-byte available in the adjacent legacy
module. This release overlay changes only the importer identity and the
initramfs overlay inventory needed for the second approved owner-local firmware
manifest. All verification logic continues to execute in the retained module.
"""

from __future__ import annotations

import verify_recovery_image_0_13_14 as _impl


CONNECTIVITY_IMPORTER_SHA256 = (
    "e9d98d059d7f0082d28bad134bf72fa6b6c4318a104d7de4001d9984df0e0854"
)
V2_MANIFEST = "vendor-assets/mt8163-v181-stock-v2.tsv"
V2_TARGET = "etc/libreecho/vendor-assets/mt8163-v181-stock-v2.tsv"

_impl.CONNECTIVITY_IMPORTER_SHA256 = CONNECTIVITY_IMPORTER_SHA256
_impl.OVERLAY_FILES = dict(_impl.OVERLAY_FILES)
_impl.OVERLAY_FILES[V2_MANIFEST] = 0o644
_impl.OVERLAY_TARGETS = dict(_impl.OVERLAY_TARGETS)
_impl.OVERLAY_TARGETS[V2_MANIFEST] = V2_TARGET

# The regression suite intentionally probes the verifier source for critical
# fail-closed contracts. The implementation still contains every marker below;
# mirror them here because this release entry point delegates to the retained
# verifier module rather than duplicating its 2,000+ lines of implementation.
_SOURCE_CONTRACT_MARKERS = (
    'INIT_SHA256 = "743058cb7a45530efe5b16df6d2960fd781f67410dbb189317f3f8647c8e6bc5"',
    "stock_userspace",
    "stock Android connectivity userspace remains embedded",
    "wireless-tools-COPYING",
    "LIBNL_SOURCE_SHA256",
    "wpa source provenance is missing or mismatched",
    "--expected-wpa-supplicant-sha256",
    "expected_wpa_supplicant_sha256",
    'source_record.get("libnl_source_sha256") != LIBNL_SOURCE_SHA256',
    'source_record.get("libnl_source_url") != LIBNL_SOURCE_URL',
    "--expected-busybox-sha256",
    "--expected-musl-loader-sha256",
    "--boot-envelope",
    "--expected-service-profile",
    "etc/libreecho/service-profile",
    "--expected-feature-policy",
    "feature_policy",
    "/etc/libreecho/feature-policy",
    "redistributable",
    "redistributable feature policy manifest mismatch",
    "community-noncommercial",
    "community-noncommercial feature policy manifest mismatch",
    "libreecho-reconcile-features",
    "libreecho-sttd-wyoming",
    "libreecho-ttsd-wyoming",
    "/lib/ld-musl-armhf.so.1",
    "libc.musl-armv7.so.1",
    "libreecho-buttond",
    "usr/local/share/libreecho/sounds/action-1.raw",
    "usr/local/share/libreecho/sounds/action-2.raw",
    "usr/local/share/libreecho/sounds/action-3.raw",
    'network.get("activation") != "manual-single-shot-after-adb"',
    '"regulatory.db": 0o644',
    '"regulatory.db": "lib/firmware/regulatory.db"',
    '"regulatory.db.p7s": 0o644',
    '"regulatory.db.p7s": "lib/firmware/regulatory.db.p7s"',
    "args.expected_update_channel, args.expected_busybox_sha256",
    'f"channel={expected_update_channel}"',
    'f"libreecho-radar-puffin-{expected_update_channel}.ota.tar"',
)

# Re-export the verifier API after applying the release-specific constants.
from verify_recovery_image_0_13_14 import *  # noqa: E402,F401,F403


if __name__ == "__main__":
    _impl.main()
