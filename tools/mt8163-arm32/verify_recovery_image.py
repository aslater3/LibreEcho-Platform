#!/usr/bin/env python3
"""Independent verifier for the MT8163 ARM32 recovery boot image."""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import re
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_boot_envelope import generate as generate_boot_envelope
from libreecho_platform_targets import add_target_arguments, validate_target_arguments, identity_bytes, get_target, audio_profile_bytes


ANDROID_MAGIC = b"ANDROID!"
RAMDISK_COMPRESSION = "xz-crc32-lzma2-8m"
RAMDISK_XZ_MAX_DICT_SIZE = 8 << 20
XZ_MAGIC = b"\xfd7zXZ\x00"
XZ_CHECK_CRC32 = b"\x00\x01"
MKIMG_MAGIC = bytes.fromhex("88168858")
FDT_MAGIC = bytes.fromhex("d00dfeed")
PAGE = 0x800
MKIMG_SIZE = 0x200
IMAGE_SIZE = 0x1000000
KERNEL_ADDR = 0x40008000
RAMDISK_ADDR = 0x43478000
RAMDISK_END_LIMIT = 0x44400000
TAGS_ADDR = 0x48000000
ATF_START = 0x43000000
ATF_END = 0x43030000
DTB_SIZE = 0x10000
ZIMAGE_MAGIC = 0x016F2818

ZIMAGE_SHA256 = "4e144959eb0ffaee91b37d05a0f871863a74f4abb1bad0474c2fec358d5176a6"
SYSTEM_MAP_SHA256 = "527292112edd28e8facf2998eefe2224b08a05b193efc73634cd998e9113ba95"
CONNECTIVITY_BUNDLE_ID = "mt8163-v181-stock-v1"
CONNECTIVITY_IMPORTER_SHA256 = "078b0b5ee29ffb952a9c2f3df64fb11ce57dc79046e48159d990a89c4905c902"
CONNECTIVITY_STOCK_SYSTEM_SHA256 = "56540b3a9ac4437901a5510d9fb5e09b1a8d0cc229548f0b08bb5c22d78684fe"
CONNECTIVITY_EVIDENCE_MANIFEST_SHA256 = "d1eedd04efe0dbc78853f2b0f9357c092b4ca66242648908c0369956538441eb"
WPA_SUPPLICANT_VERSION = "2.10"
WPA_SOURCE_SHA256 = "20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f"
WPA_SOURCE_URL = "https://w1.fi/releases/wpa_supplicant-2.10.tar.gz"
LIBNL_SOURCE_SHA256 = "2a56e1edefa3e68a7c00879496736fdbf62fc94ed3232c0baba127ecfa76874d"
LIBNL_SOURCE_URL = (
    "https://github.com/thom311/libnl/releases/download/"
    "libnl3_11_0/libnl-3.11.0.tar.gz"
)
WIRELESS_TOOLS_VERSION = "30~pre9"
WIRELESS_TOOLS_SOURCE_SHA256 = "abd9c5c98abf1fdd11892ac2f8a56737544fe101e1be27c6241a564948f34c63"
WIRELESS_TOOLS_SOURCE_URL = "https://archive.ubuntu.com/ubuntu/pool/main/w/wireless-tools/wireless-tools_30~pre9.orig.tar.gz"

INIT_SHA256 = "7cea4e75f896767e314585726b683a132988d1d7d18db65ba8aea387172cbf3b"
BOOT_ENVELOPE_SHA256 = "e83e11b9ef8338cf3262144870790d2b005df16baf4d119849658943e64bbf7a"
OVERLAY_FILES = {
    "default.prop": 0o644,
    "profile": 0o644,
    "init.rc": 0o644,
    "init.recovery.mt8163.rc": 0o644,
    "libreecho-init": 0o755,
    "libreecho-recovery-ap-probe": 0o755,
    "libreecho-recovery-ap-ready": 0o755,
    "libreecho-recovery-net-up": 0o755,
    "libreecho-recovery-net-down": 0o755,
    "libreecho-mdnsd": 0o755,
    "libreecho-reconcile-features": 0o755,
    "libreecho-data-cleanup": 0o755,
    "libreecho-vendor-import": 0o755,
    "vendor-assets/mt8163-v181-stock-v1.tsv": 0o644,
    "vendor-assets/mt8163-v181-stock-v2.tsv": 0o644,
    "vendor-assets/mt8163-v181-stock-v3.tsv": 0o644,
    "libreecho-update": 0o755,
    "libreecho-update-fetch": 0o755,
    "libreecho-feature-transaction": 0o755,
    "libreecho-target-manifest": 0o755,
    "libreecho-generation": 0o755,
    "libreecho-config-migrate": 0o755,
    "libreecho-generation-transaction": 0o755,
    "ota-source.conf": 0o644,
    "regulatory.db": 0o644,
    "regulatory.db.p7s": 0o644,
}
OVERLAY_TARGETS = {
    "profile": "etc/profile",
    "libreecho-mdnsd": "etc/init.d/libreecho-mdnsd.init",
    "libreecho-recovery-ap-probe": "usr/local/sbin/libreecho-recovery-ap-probe",
    "libreecho-recovery-ap-ready": "usr/local/sbin/libreecho-recovery-ap-ready",
    "libreecho-recovery-net-up": "usr/local/sbin/libreecho-recovery-net-up",
    "libreecho-recovery-net-down": "usr/local/sbin/libreecho-recovery-net-down",
    "libreecho-reconcile-features": "usr/local/sbin/libreecho-reconcile-features",
    "libreecho-data-cleanup": "usr/local/sbin/libreecho-data-cleanup",
    "libreecho-vendor-import": "usr/local/sbin/libreecho-vendor-import",
    "vendor-assets/mt8163-v181-stock-v1.tsv": (
        "etc/libreecho/vendor-assets/mt8163-v181-stock-v1.tsv"
    ),
    "vendor-assets/mt8163-v181-stock-v2.tsv": (
        "etc/libreecho/vendor-assets/mt8163-v181-stock-v2.tsv"
    ),
    "vendor-assets/mt8163-v181-stock-v3.tsv": (
        "etc/libreecho/vendor-assets/mt8163-v181-stock-v3.tsv"
    ),
    "libreecho-update": "usr/local/sbin/libreecho-update",
    "libreecho-update-fetch": "usr/local/sbin/libreecho-update-fetch",
    "libreecho-feature-transaction": "usr/local/sbin/libreecho-feature-transaction",
    "libreecho-target-manifest": "usr/local/sbin/libreecho-target-manifest",
    "libreecho-generation": "usr/local/sbin/libreecho-generation",
    "libreecho-config-migrate": "usr/local/sbin/libreecho-config-migrate",
    "libreecho-generation-transaction": "usr/local/sbin/libreecho-generation-transaction",
    "ota-source.conf": "etc/libreecho/ota-source.conf",
    "regulatory.db": "lib/firmware/regulatory.db",
    "regulatory.db.p7s": "lib/firmware/regulatory.db.p7s",
}
# Pinned recovery access-point bundle (issue #96).  The shell probes and the
# interface helpers ship via the overlay; hostapd/dnsmasq/iw and the compiled
# evdev button detector ship as hash-pinned components staged by
# build_recovery_image.py --recovery-ap-binaries/--recovery-ap-metadata.
RECOVERY_AP_COMPONENTS = {
    "hostapd": "usr/local/sbin/hostapd",
    "dnsmasq": "usr/local/sbin/dnsmasq",
    "iw": "usr/local/sbin/iw",
    "libreecho-recovery-button": "usr/local/sbin/libreecho-recovery-button",
}
RECOVERY_AP_OVERLAY = {
    "ap_probe": "usr/local/sbin/libreecho-recovery-ap-probe",
    "ready_probe": "usr/local/sbin/libreecho-recovery-ap-ready",
    "net_up": "usr/local/sbin/libreecho-recovery-net-up",
    "net_down": "usr/local/sbin/libreecho-recovery-net-down",
}
RECOVERY_AP_MARKER = "/run/libreecho/recovery-mode"
RECOVERY_AP_METADATA = "etc/libreecho/recovery-ap-binaries.json"
RECOVERY_AP_METADATA_SCHEMA = "libreecho-recovery-ap-binaries/v1"
# The checked-in pin document the on-image metadata must agree with.  The
# metadata is a self-declared JSON document, so the verifier refuses any
# component whose name/version/source URL/source SHA-256/licence does not match
# this file; a hand-written document cannot pass off an unlisted component.
RECOVERY_AP_LOCK_PATH = Path(__file__).resolve().parent / "recovery-ap" / "SOURCE.lock"
RECOVERY_AP_LICENSE_ROOT = "usr/local/share/licenses/libreecho-core"
SSH_MEMBER_NAMES = {
    "sbin/dropbear", "sbin/dropbearkey", "usr/bin/scp", "etc/passwd", "etc/group",
    "etc/shells", "etc/init.d/libreecho-ssh.init",
}
UI_BINARY_NAMES = {
    "usr/local/sbin/libreecho-web",
    "usr/local/sbin/libreecho-logd",
    "usr/local/sbin/libreecho-networkd",
    "usr/local/sbin/libreecho-timed",
    "usr/local/sbin/libreecho-timerd",
    "usr/local/sbin/libreecho-watchdogd",
    "usr/local/sbin/libreecho-audiod",
    "usr/local/sbin/libreecho-micd",
    "usr/local/sbin/libreecho-ledd",
    "usr/local/sbin/libreecho-buttond",
    "usr/local/sbin/libreecho-radiod",
    "usr/local/sbin/libreecho-btd",
    "usr/local/sbin/libreecho-airplayd",
    "usr/local/sbin/libreecho-esphomed",
    "usr/local/sbin/libreecho-sttd-wyoming",
    "usr/local/sbin/libreecho-ttsd-wyoming",
    "usr/local/sbin/libreecho-mdnsd",
}
UI_INIT_NAMES = {
    "etc/init.d/libreecho-web.init",
    "etc/init.d/libreecho-logd.init",
    "etc/init.d/libreecho-networkd.init",
    "etc/init.d/libreecho-timed.init",
    "etc/init.d/libreecho-timerd.init",
    "etc/init.d/libreecho-watchdogd.init",
    "etc/init.d/libreecho-audiod.init",
    "etc/init.d/libreecho-micd.init",
    "etc/init.d/libreecho-ledd.init",
    "etc/init.d/libreecho-buttond.init",
    "etc/init.d/libreecho-radiod.init",
    "etc/init.d/libreecho-btd.init",
    "etc/init.d/libreecho-airplayd.init",
    "etc/init.d/libreecho-ttsd.init",
    "etc/init.d/libreecho-waked.init",
    "etc/init.d/libreecho-sttd.init",
    "etc/init.d/libreecho-agentd.init",
    "etc/init.d/libreecho-esphomed.init",
}
UI_FIXED_NAMES = UI_BINARY_NAMES | UI_INIT_NAMES | {
    "etc/libreecho/web-config.json",
    "etc/libreecho/airplay2.conf",
    "etc/libreecho/ntp.conf",
    "etc/libreecho/avahi-services/esphome.service",
    "usr/local/share/libreecho/ui-manifest.txt",
    "usr/local/share/libreecho/sounds/action-1.raw",
    "usr/local/share/libreecho/sounds/action-2.raw",
    "usr/local/share/libreecho/sounds/action-3.raw",
}
UI_OPTIONAL_NAMES = {"etc/libreecho/users"}
AIRPLAY_BINARY_NAMES = {
    "usr/local/sbin/nqptp", "usr/local/sbin/shairport-sync",
    "usr/local/sbin/avahi-daemon", "usr/local/sbin/dbus-daemon",
    "usr/local/sbin/libreecho-airplay-audio",
    "usr/local/sbin/libreecho-audio-engine",
}
MDNS_CONTRACT_SCHEMA = "libreecho-mdns-runtime-contract/v1"
MDNS_RUNTIME_SCHEMA = "libreecho-mdns-runtime/v1"
MDNS_MARKER_SCHEMA = "libreecho-mdns-runtime-marker/v1"
MDNS_RUNTIME_PREFIX = "usr/local/lib/libreecho-mdns/root/"
MDNS_MARKER = "etc/libreecho/mdns-runtime.json"
MDNS_INIT = "etc/init.d/libreecho-mdnsd.init"
MDNS_CONTRACT_CATEGORIES = ("executables", "libraries", "config", "accounts", "licenses")

CONNECTIVITY_ASSET_REQUIREMENTS: dict[str, dict[str, str | int]] = {
    "ROMv2_lm_patch_1_0_hdr.bin": {
        "source": "etc/firmware/ROMv2_lm_patch_1_0_hdr.bin",
        "size": 127596,
        "sha256": "36d7edc7095f4cdfdaaa9c67061cf079199a55be85ab76ce82c9e9bcb34824a2",
    },
    "ROMv2_lm_patch_1_1_hdr.bin": {
        "source": "etc/firmware/ROMv2_lm_patch_1_1_hdr.bin",
        "size": 50952,
        "sha256": "cabdb842d354dd123d2d3d939f06304c1cfb045f407b5880444be326ded16d8d",
    },
    "WIFI_RAM_CODE_8163": {
        "source": "etc/firmware/WIFI_RAM_CODE_8163",
        "size": 373840,
        "sha256": "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c",
    },
    "WMT_SOC.cfg": {
        "source": "etc/firmware/WMT_SOC.cfg",
        "size": 119,
        "sha256": "302bd4462de99c028c04092e561c1500d65582ce42a93c4c72ccae6e2c99013d",
    },
}

CONNECTIVITY_HELPERS = {
    "sbin/wmt_configure": (
        25744, "e0ff85f0ac2cb2b98718556470444cafd1fcd8865cdba27aa67e2c7d7a3303e0",
    ),
    "sbin/wmt_responder": (
        21648, "46170ddc1d1ddf21a85ec16df129aac47a258a439bc9e6ed061d1e5942aa48eb",
    ),
    "sbin/wmt_bt_on": (
        21648, "985320b270149cd27bc59d7f34d0da829817f225a4e712037633517c843cc745",
    ),
    "sbin/wmt_stock_compat": (
        21648, "7e3afe31b706029ebf6e271f5cda6e3880cfc5b184abb052a190662759708c87",
    ),
    "sbin/wmt_launcher": (
        21648, "65cb5c0c49bb61aec657c114cf67269e398bf41ff7b70a4abb8eb0ec36ff2c99",
    ),
}

CONNECTIVITY_RUNTIME_SYMLINKS = {
    "etc/firmware": "../lib/firmware",
}


def fail(message: str) -> None:
    raise SystemExit("ERROR: " + message)


def validate_mkimg_header(kernel: bytes) -> None:
    """Validate the MediaTek mkimg header wrapping the kernel payload.

    LK compares the name field as a null-terminated C string; a header
    whose name bytes are correct but lack a trailing NUL (e.g. 0xFF fill)
    will be rejected with "KERNEL partition name not match" and the DTB
    will never be located.
    """
    if kernel[:4] != MKIMG_MAGIC or kernel[8:14] != b"KERNEL":
        fail("MediaTek KERNEL header missing")
    if kernel[14] != 0:
        fail("MediaTek KERNEL header name not null-terminated (LK rejects this)")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strictly_equal(actual: object, expected: object) -> bool:
    """Compare JSON-shaped values without accepting bool as an integer."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            strictly_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            strictly_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")


def manifest_schema(manifest: dict[str, object]) -> int:
    schema_version = manifest.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in (1, 2):
        fail(f"unsupported manifest schema version: {schema_version!r}")
    return schema_version


def align(value: int) -> int:
    return (value + PAGE - 1) & ~(PAGE - 1)


def android_id(kernel: bytes, ramdisk: bytes, second: bytes, dt: bytes) -> bytes:
    digest = hashlib.sha1()
    for blob in (kernel, ramdisk, second):
        digest.update(blob)
        digest.update(struct.pack("<I", len(blob)))
    if dt:
        digest.update(dt)
        digest.update(struct.pack("<I", len(dt)))
    return digest.digest().ljust(32, b"\0")


@dataclass(frozen=True)
class Entry:
    name: str
    mode: int
    uid: int
    gid: int
    mtime: int
    data: bytes


def decompress_ramdisk(ramdisk: bytes) -> bytes:
    """Accept only a single-stream CRC32 xz the target kernel can unpack.

    The Linux xz_dec used for initramfs rejects CRC64/SHA-256 checks, so an
    archive Python can read may still fail at boot; enforce the subset here.
    """
    if ramdisk[:6] != XZ_MAGIC:
        fail("ramdisk is not an xz stream")
    if ramdisk[6:8] != XZ_CHECK_CRC32:
        fail("ramdisk xz integrity check must be CRC32 for the kernel decoder")
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=RAMDISK_XZ_MAX_DICT_SIZE * 2)
    try:
        cpio = decoder.decompress(ramdisk)
    except lzma.LZMAError as exc:
        fail(f"ramdisk xz is invalid: {exc}")
    if not decoder.eof or decoder.unused_data:
        fail("ramdisk xz stream is truncated or has trailing data")
    return cpio


def parse_newc(data: bytes) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    offset = 0
    trailer = False
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        if header[:6] != b"070701":
            if trailer and not any(data[offset:]):
                break
            fail(f"invalid newc magic at {offset:#x}")
        try:
            values = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
        except ValueError:
            fail(f"invalid newc header at {offset:#x}")
        mode, uid, gid, mtime = values[1], values[2], values[3], values[5]
        size, namesize = values[6], values[11]
        offset += 110
        name_blob = data[offset:offset + namesize]
        if len(name_blob) != namesize or not name_blob.endswith(b"\0"):
            fail("truncated newc filename")
        try:
            name = name_blob[:-1].decode("utf-8")
        except UnicodeDecodeError:
            fail("non-UTF-8 newc filename")
        offset = (offset + namesize + 3) & ~3
        payload = data[offset:offset + size]
        if len(payload) != size:
            fail(f"truncated newc payload for {name}")
        offset = (offset + size + 3) & ~3
        if name == "TRAILER!!!":
            if trailer:
                fail("duplicate newc trailer")
            trailer = True
            continue
        if trailer:
            fail("newc entry follows trailer")
        normalized = name[2:] if name.startswith("./") else name
        components = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or "\0" in normalized
            or any(component in ("", ".", "..") for component in components)
        ):
            fail(f"unsafe initramfs path {name!r}")
        if normalized in entries:
            fail(f"duplicate initramfs path {normalized}")
        entries[normalized] = Entry(normalized, mode, uid, gid, mtime, payload)
    if not trailer:
        fail("newc trailer missing")
    if any(data[offset:]):
        fail("nonzero data follows newc trailer")
    return entries


def elf_info(
    data: bytes,
) -> tuple[int, int, int | None, str | None, tuple[str, ...], bool] | None:
    if data[:4] != b"\x7fELF":
        return None
    if len(data) < 20:
        fail("truncated ELF member")
    elf_class = data[4]
    if data[5] != 1:
        fail("non-little-endian ELF member")
    machine = struct.unpack_from("<H", data, 18)[0]
    if elf_class != 1:
        return elf_class, machine, None, None, (), False
    if len(data) < 52:
        fail("truncated ELF32 member")
    phoff, shoff = struct.unpack_from("<II", data, 28)
    flags = struct.unpack_from("<I", data, 36)[0]
    phentsize, phnum, shentsize, shnum = struct.unpack_from("<HHHH", data, 42)
    interpreter = None
    has_dynamic = False
    for index in range(phnum):
        start = phoff + index * phentsize
        if start + 32 > len(data):
            fail("truncated ELF program headers")
        kind, file_offset = struct.unpack_from("<II", data, start)
        file_size = struct.unpack_from("<I", data, start + 16)[0]
        if kind == 2:
            has_dynamic = True
        if kind == 3:
            raw_interpreter = data[file_offset:file_offset + file_size]
            if len(raw_interpreter) != file_size:
                fail("truncated ELF interpreter")
            try:
                interpreter = raw_interpreter.rstrip(b"\0").decode("ascii")
            except UnicodeDecodeError:
                fail("non-ASCII ELF interpreter")

    sections: list[tuple[int, int, int, int, int]] = []
    for index in range(shnum):
        start = shoff + index * shentsize
        if start + 40 > len(data):
            fail("truncated ELF section headers")
        section_type = struct.unpack_from("<I", data, start + 4)[0]
        file_offset, size, link = struct.unpack_from("<III", data, start + 16)
        entry_size = struct.unpack_from("<I", data, start + 36)[0]
        sections.append((section_type, file_offset, size, link, entry_size))

    needed: list[str] = []
    for section_type, file_offset, size, link, entry_size in sections:
        if section_type != 6:
            continue
        if link >= len(sections):
            fail("ELF dynamic section has invalid string-table link")
        _str_type, str_offset, str_size, _str_link, _str_entry = sections[link]
        strings = data[str_offset:str_offset + str_size]
        dynamic_data = data[file_offset:file_offset + size]
        if len(strings) != str_size or len(dynamic_data) != size:
            fail("truncated ELF dynamic or string-table section")
        if entry_size not in (0, 8):
            fail("unexpected ELF32 dynamic entry size")
        for offset in range(0, len(dynamic_data) - 7, 8):
            tag, value = struct.unpack_from("<II", dynamic_data, offset)
            if tag == 0:
                break
            if tag != 1:
                continue
            if value >= len(strings):
                fail("ELF DT_NEEDED string lies outside its table")
            end = strings.find(b"\0", value)
            if end < 0:
                fail("unterminated ELF DT_NEEDED string")
            try:
                needed.append(strings[value:end].decode("ascii"))
            except UnicodeDecodeError:
                fail("non-ASCII ELF DT_NEEDED string")
    return elf_class, machine, flags, interpreter, tuple(needed), has_dynamic


def require_member(entries: dict[str, Entry], name: str, expected_hash: str,
                   permissions: int) -> Entry:
    if name not in entries:
        fail(f"initramfs lacks {name}")
    entry = entries[name]
    if not stat.S_ISREG(entry.mode) or stat.S_IMODE(entry.mode) != permissions:
        fail(f"wrong mode/type for {name}: {entry.mode:#o}")
    if sha256(entry.data) != expected_hash:
        fail(f"hash mismatch for initramfs member {name}")
    return entry


def resolve_relative_symlink(name: str, target: str) -> str:
    components = target.split("/")
    if (
        not target
        or target.startswith("/")
        or "\0" in target
        or any(component in ("", ".") for component in components)
    ):
        fail(f"unsafe initramfs symlink: {name} -> {target!r}")
    parts = list(PurePosixPath(name).parent.parts)
    if parts == ["."]:
        parts = []
    for component in components:
        if component == "..":
            if not parts:
                fail(f"initramfs symlink escapes archive root: {name} -> {target}")
            parts.pop()
        else:
            parts.append(component)
    resolved = "/".join(parts)
    if not resolved:
        fail(f"initramfs symlink resolves to archive root: {name} -> {target}")
    return resolved


def validate_archive_tree(entries: dict[str, Entry]) -> None:
    for name in entries:
        parts = PurePosixPath(name).parts
        for count in range(1, len(parts)):
            parent = "/".join(parts[:count])
            entry = entries.get(parent)
            if entry is None or not stat.S_ISDIR(entry.mode):
                fail(f"initramfs member {name} has a missing or non-directory parent {parent}")


def validate_symlinks(entries: dict[str, Entry]) -> None:
    for name, entry in entries.items():
        if not stat.S_ISLNK(entry.mode):
            continue
        current = name
        seen: set[str] = set()
        while stat.S_ISLNK(entries[current].mode):
            if current in seen:
                fail(f"initramfs symlink loop includes {current}")
            seen.add(current)
            try:
                target = entries[current].data.decode("utf-8")
            except UnicodeDecodeError:
                fail(f"non-UTF-8 initramfs symlink target for {current}")
            current = resolve_relative_symlink(current, target)
            if current not in entries:
                fail(f"dangling initramfs symlink: {name} -> {current}")
        target_entry = entries[current]
        if not (stat.S_ISREG(target_entry.mode) or stat.S_ISDIR(target_entry.mode)):
            fail(f"initramfs symlink has unsupported target type: {name} -> {current}")


def validate_no_connectivity_autostart(entries: dict[str, Entry]) -> None:
    if "init.connectivity.rc" in entries:
        fail("auto-starting init.connectivity.rc entered the initramfs")
    forbidden_launches = (
        b"wmt_loader", b"wmt_launcher", b"wmt_configure", b"wmt_responder", b"wmt_bt_on",
    )
    forbidden_wifi_writes = (
        b"> /dev/wmtWifi", b">/dev/wmtWifi", b"tee /dev/wmtWifi", b"of=/dev/wmtWifi",
    )
    active_controls = sorted(
        name for name in entries if name.endswith(".rc") or name == "libreecho-init"
    )
    for name in active_controls:
        control = entries[name].data
        forbidden = () if name == "libreecho-init" else forbidden_launches + forbidden_wifi_writes
        for marker in forbidden:
            if marker in control:
                fail(f"active recovery control {name} contains {marker!r}")
        for line in control.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[:2] == [b"write", b"/dev/wmtWifi"]:
                fail(f"active recovery control {name} activates Wi-Fi through Android init")


def validate_ssh(entries: dict[str, Entry], manifest: dict[str, object],
                 expected_dropbear_sha256: str | None,
                 expected_dropbearkey_sha256: str | None,
                 expected_scp_sha256: str | None) -> bool:
    raw_ssh = manifest.get("ssh")
    if raw_ssh is None:
        ssh: dict[str, object] = {"enabled": False}
    elif not isinstance(raw_ssh, dict) or not isinstance(raw_ssh.get("enabled"), bool):
        fail("SSH manifest record is malformed")
    else:
        ssh = cast(dict[str, object], raw_ssh)

    if "etc/shadow" in entries:
        fail("SSH image contains forbidden /etc/shadow credential material")
    forbidden_ssh_names = sorted(
        name for name in entries
        if name.endswith("/authorized_keys") or name == "authorized_keys"
        or "/.ssh/" in name or name.endswith(("/id_rsa", "/id_ecdsa", "/id_ed25519"))
    )
    if forbidden_ssh_names:
        fail(f"SSH image contains forbidden key material: {forbidden_ssh_names}")

    expected_enabled = (
        expected_dropbear_sha256 is not None or
        expected_dropbearkey_sha256 is not None or
        expected_scp_sha256 is not None
    )
    if bool(ssh.get("enabled")) != expected_enabled:
        fail(
            "SSH bundle expectation mismatch: "
            f"expected={'enabled' if expected_enabled else 'disabled'} "
            f"actual={'enabled' if ssh.get('enabled') else 'disabled'}"
        )

    if not expected_enabled:
        unexpected = sorted(name for name in SSH_MEMBER_NAMES if name in entries)
        if unexpected:
            fail(f"SSH bundle is disabled but members are present: {unexpected}")
        if any(name.startswith("etc/dropbear/") for name in entries):
            fail("SSH image contains persistent host-key material")
        return False

    if (expected_dropbear_sha256 is None or
            expected_dropbearkey_sha256 is None or
            expected_scp_sha256 is None):
        fail("SSH binary identities are incomplete")
    assert expected_dropbear_sha256 is not None
    assert expected_dropbearkey_sha256 is not None
    assert expected_scp_sha256 is not None
    expected_policy = {
        "enabled": True,
        "activation": "deferred-after-webui-bootstrap",
        "autostart": True,
        "authentication": "webui-users-sha256",
        "account_source": "/data/libreecho/config/users",
        "privilege_policy": "non-root-ephemeral-users",
        "public_key_auth": False,
        "root_login": False,
        "host_keys": "generated-ephemerally-under-/tmp/dropbear",
    }
    for key, value in expected_policy.items():
        if ssh.get(key) != value:
            fail(f"SSH policy changed for {key}: {ssh.get(key)!r}")
    raw_files = ssh.get("files")
    if not isinstance(raw_files, dict):
        fail("SSH file manifest is missing")
    files = cast(dict[str, object], raw_files)
    if set(files) != SSH_MEMBER_NAMES:
        fail("SSH file manifest members changed")

    def static_binary_record(name: str, expected_hash: str) -> None:
        raw_record = files.get(name)
        if not isinstance(raw_record, dict):
            fail(f"SSH binary manifest record is missing: {name}")
        record = cast(dict[str, object], raw_record)
        source_path = record.get("path")
        if not isinstance(source_path, str) or not Path(source_path).is_absolute():
            fail(f"SSH binary manifest path is not absolute: {name}")
        member = require_member(entries, name, expected_hash, 0o755)
        if elf_info(member.data) != (1, 40, 0x05000400, None, (), False):
            fail(f"SSH binary is not static ARM32 hard-float: {name}")
        if b"authorized_keys" in member.data:
            fail(f"public-key authorization marker found in {name}")
        expected_record = {
            "path": source_path,
            "sha256": expected_hash,
            "size": len(member.data),
            "mode": "0755",
            "elf": {
                "class": 1,
                "machine": 40,
                "flags": "0x05000400",
                "interpreter": None,
                "needed": [],
                "dynamic": False,
            },
        }
        if record != expected_record:
            fail(f"SSH binary manifest record mismatch: {name}")

    static_binary_record("sbin/dropbear", expected_dropbear_sha256)
    static_binary_record("sbin/dropbearkey", expected_dropbearkey_sha256)
    static_binary_record("usr/bin/scp", expected_scp_sha256)

    expected_accounts = {
        "etc/passwd": b"root:x:0:0:root:/root:/bin/sh\n",
        "etc/group": b"root:x:0:\nlibreecho-ssh:x:1000:\n",
        "etc/shells": b"/bin/sh\n",
    }
    for name, data in expected_accounts.items():
        member = require_member(entries, name, sha256(data), 0o644)
        record = files.get(name)
        if record != {
            "path": "/" + name,
            "sha256": sha256(data),
            "size": len(data),
            "mode": "0644",
        }:
            fail(f"SSH account manifest record mismatch: {name}")
        if member.data != data:
            fail(f"SSH account content changed: {name}")

    supervisor = entries.get("etc/init.d/libreecho-ssh.init")
    raw_supervisor = files.get("etc/init.d/libreecho-ssh.init")
    if (supervisor is None or not stat.S_ISREG(supervisor.mode) or
            stat.S_IMODE(supervisor.mode) != 0o755 or
            not isinstance(raw_supervisor, dict)):
        fail("SSH supervisor is missing or has unsafe permissions")
    assert supervisor is not None
    assert isinstance(raw_supervisor, dict)
    supervisor_record = cast(dict[str, object], raw_supervisor)
    supervisor_path = supervisor_record.get("path")
    if not isinstance(supervisor_path, str) or not Path(supervisor_path).is_absolute():
        fail("SSH supervisor manifest path is not absolute")
    if supervisor_record != {
        "path": supervisor_path,
        "sha256": sha256(supervisor.data),
        "size": len(supervisor.data),
        "mode": "0755",
    }:
        fail("SSH supervisor manifest record mismatch")
    if (b"/etc/shadow" in supervisor.data or
            b"ssh-root-password-hash" in supervisor.data or
            b"authorized_keys" in supervisor.data):
        fail("SSH supervisor contains forbidden credential or key material")
    if any(name.startswith("etc/dropbear/") for name in entries):
        fail("SSH image contains persistent host-key material")
    return True


def validate_network_tools(entries: dict[str, Entry], manifest: dict[str, object],
                           expected_iwconfig_sha256: str | None) -> bool:
    raw_tools = manifest.get("network_tools", {"enabled": False})
    if not isinstance(raw_tools, dict) or not isinstance(raw_tools.get("enabled"), bool):
        fail("network-tools manifest record is malformed")
    network_tools = cast(dict[str, object], raw_tools)
    member_names = {"sbin/ifconfig", "sbin/iwconfig"}

    if expected_iwconfig_sha256 is None:
        if network_tools.get("enabled") or any(name in entries for name in member_names):
            fail("network tools are present without an expected iwconfig identity")
        return False

    if not network_tools.get("enabled"):
        fail("network tools are expected but the manifest is disabled")
    if network_tools.get("activation") != "manual-only":
        fail("network-tools activation policy changed")
    if network_tools.get("autostart") is not False:
        fail("network-tools autostart policy changed")
    raw_records = network_tools.get("tools")
    if not isinstance(raw_records, dict) or set(raw_records) != {"ifconfig", "iwconfig"}:
        fail("network-tools manifest members changed")
    records = cast(dict[str, object], raw_records)

    ifconfig = entries.get("sbin/ifconfig")
    if (ifconfig is None or not stat.S_ISLNK(ifconfig.mode) or
            stat.S_IMODE(ifconfig.mode) != 0o777 or ifconfig.data != b"../bin/ifconfig"):
        fail("/sbin/ifconfig symlink contract changed")
    bin_ifconfig = entries.get("bin/ifconfig")
    if (bin_ifconfig is None or not stat.S_ISLNK(bin_ifconfig.mode) or
            bin_ifconfig.data != b"busybox"):
        fail("BusyBox ifconfig provider changed")
    if records.get("ifconfig") != {
        "path": "/sbin/ifconfig",
        "provider": "busybox",
        "target": "../bin/ifconfig",
        "mode": "0777",
    }:
        fail("ifconfig manifest record changed")

    raw_iwconfig = records.get("iwconfig")
    if not isinstance(raw_iwconfig, dict):
        fail("iwconfig manifest record is missing")
    iwconfig_record = cast(dict[str, object], raw_iwconfig)
    raw_source = iwconfig_record.get("source")
    required_source = {
        "binary_sha256", "binary_size", "build_epoch", "compiler",
        "kernel_uapi_sha256", "license", "license_file", "license_sha256",
        "source_sha256", "source_url", "static", "version",
    }
    if not isinstance(raw_source, dict) or set(raw_source) != required_source:
        fail("wireless-tools source provenance is missing or malformed")
    source = cast(dict[str, object], raw_source)
    if (source.get("binary_sha256") != expected_iwconfig_sha256 or
            source.get("binary_size") != len(require_member(
                entries, "sbin/iwconfig", expected_iwconfig_sha256, 0o755,
            ).data) or
            source.get("license") != "GPL-2.0-only AND LGPL-2.1-or-later" or
            source.get("license_file") != "wireless-tools-COPYING" or
            source.get("source_sha256") != WIRELESS_TOOLS_SOURCE_SHA256 or
            source.get("source_url") != WIRELESS_TOOLS_SOURCE_URL or
            source.get("static") is not True or
            source.get("version") != WIRELESS_TOOLS_VERSION or
            not re.fullmatch(r"[0-9a-f]{64}", str(source.get("kernel_uapi_sha256", ""))) or
            not re.fullmatch(r"[0-9a-f]{64}", str(source.get("license_sha256", ""))) or
            "/home/" in str(source.get("compiler", ""))):
        fail("wireless-tools source provenance is invalid")
    license_member = require_member(
        entries, "usr/local/share/licenses/libreecho-core/wireless-tools-COPYING",
        str(source["license_sha256"]), 0o644,
    )
    if sha256(license_member.data) != source["license_sha256"]:
        fail("wireless-tools license identity changed")
    iwconfig_path = iwconfig_record.get("path")
    if not isinstance(iwconfig_path, str) or not Path(iwconfig_path).is_absolute():
        fail("iwconfig manifest path is not absolute")
    iwconfig = require_member(entries, "sbin/iwconfig", expected_iwconfig_sha256, 0o755)
    if elf_info(iwconfig.data) != (1, 40, 0x05000400, None, (), False):
        fail("iwconfig is not static ARM32 hard-float")
    if iwconfig_record != {
        "path": iwconfig_path,
        "sha256": expected_iwconfig_sha256,
        "size": len(iwconfig.data),
        "mode": "0755",
        "elf": {
            "class": 1,
            "machine": 40,
            "flags": "0x05000400",
            "interpreter": None,
            "needed": [],
            "dynamic": False,
        },
        "source": source,
    }:
        fail("iwconfig manifest record changed")
    return True


def validate_ui(entries: dict[str, Entry], manifest: dict[str, object],
                expected_manifest_sha256: str | None,
                expected_commit: str | None,
                expected_diff_sha256: str | None) -> bool:
    for name, entry in entries.items():
        if ((name.endswith(".service") and b"_wyoming._tcp" in entry.data) or
                name.startswith("usr/local/sbin/libreecho-wyomingd") or
                name.startswith("etc/init.d/libreecho-wyomingd") or
                name.endswith("/wyoming.service") or
                name.startswith("etc/libreecho/esphome-") or
                (name.startswith("usr/local/sbin/libreecho-esphomed") and
                 name != "usr/local/sbin/libreecho-esphomed")):
            fail(f"forbidden obsolete satellite or private ESPHome file: {name}")
    raw_ui = manifest.get("ui", {"enabled": False})
    if not isinstance(raw_ui, dict) or not isinstance(raw_ui.get("enabled"), bool):
        fail("UI manifest record is malformed")
    ui = cast(dict[str, object], raw_ui)
    expected_enabled = expected_manifest_sha256 is not None
    if bool(ui.get("enabled")) != expected_enabled:
        fail(
            "UI bundle expectation mismatch: "
            f"expected={'enabled' if expected_enabled else 'disabled'} "
            f"actual={'enabled' if ui.get('enabled') else 'disabled'}"
        )

    actual_ui_files = {
        name for name, entry in entries.items()
        if stat.S_ISREG(entry.mode) and (
            name in UI_FIXED_NAMES or
            name in UI_OPTIONAL_NAMES or
            name.startswith("usr/local/share/libreecho/web/")
        )
    }
    if not expected_enabled:
        if actual_ui_files:
            fail(f"UI bundle is disabled but members are present: {sorted(actual_ui_files)}")
        return False

    if expected_commit is None or expected_diff_sha256 is None:
        fail("UI source identities are incomplete")
    expected_policy = {
        "enabled": True,
        "activation": "automatic-after-loopback",
        "autostart": True,
        "hardware_ownership": "existing-control-plane",
        "commit": expected_commit,
        "diff_sha256": expected_diff_sha256,
        "manifest_sha256": expected_manifest_sha256,
    }
    for key, value in expected_policy.items():
        if ui.get(key) != value:
            fail(f"UI policy changed for {key}: {ui.get(key)!r}")

    raw_files = ui.get("files")
    if not isinstance(raw_files, dict):
        fail("UI file manifest record is missing")
    files = cast(dict[str, object], raw_files)
    if any(name not in UI_FIXED_NAMES | UI_OPTIONAL_NAMES and
           not name.startswith("usr/local/share/libreecho/web/") for name in files):
        fail("UI file set has unexpected members")
    if set(files) != actual_ui_files or not UI_FIXED_NAMES.issubset(files):
        fail("UI file set changed")
    if not any(name.startswith("usr/local/share/libreecho/web/") for name in files):
        fail("UI web asset set is empty")

    for name, raw_record in files.items():
        if not isinstance(raw_record, dict):
            fail(f"UI file record is malformed: {name}")
        record = cast(dict[str, object], raw_record)
        digest = record.get("sha256")
        size = record.get("size")
        mode = record.get("mode")
        source = record.get("source")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                not isinstance(size, int) or not isinstance(mode, str) or
                not isinstance(source, str)):
            fail(f"UI file record is invalid: {name}")
        expected_mode = 0o755 if name in UI_BINARY_NAMES | UI_INIT_NAMES else (
            0o600 if name in {"etc/libreecho/web-config.json", "etc/libreecho/users"} else 0o644
        )
        if mode != f"{expected_mode:04o}":
            fail(f"UI file mode changed: {name}")
        member = require_member(entries, name, digest, expected_mode)
        if len(member.data) != size:
            fail(f"UI file size changed: {name}")
        if name == "etc/libreecho/users" and not member.data.strip():
            fail("UI users file is empty")
        if name.startswith("usr/local/share/libreecho/sounds/") and not member.data:
            fail(f"UI action sound is empty: {name}")
        if name in UI_BINARY_NAMES:
            if name in {
                    "usr/local/sbin/libreecho-sttd-wyoming",
                    "usr/local/sbin/libreecho-ttsd-wyoming"}:
                expected_elf = (
                    1, 40, 0x05000400, "/lib/ld-musl-armhf.so.1",
                    ("libc.musl-armv7.so.1",), True,
                )
            else:
                expected_elf = (1, 40, 0x05000400, None, (), False)
            if elf_info(member.data) != expected_elf:
                fail(f"UI binary ARM32 ABI contract changed: {name}")

    return True


def mdns_contract() -> dict[str, Any]:
    """Read the checked-in shared mDNS runtime contract."""
    path = Path(__file__).resolve().parent / "mdns" / "runtime-contract.json"
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: mDNS runtime contract is unreadable: {path}") from exc
    if not isinstance(document, dict) or document.get("schema") != MDNS_CONTRACT_SCHEMA:
        raise SystemExit("ERROR: mDNS runtime contract schema changed")
    return document


def validate_mdns(entries: dict[str, Entry], manifest: dict[str, object],
                  expected_mdns_runtime_manifest_sha256: str | None,
                  expected_service_profile: str) -> bool:
    """Fail closed on a missing loader, library, executable, config or license."""
    raw_mdns = manifest.get("mdns", {"enabled": False})
    if not isinstance(raw_mdns, dict) or not isinstance(raw_mdns.get("enabled"), bool):
        fail("mDNS manifest record is malformed")
    mdns = cast(dict[str, object], raw_mdns)
    contract = mdns_contract()
    present = sorted(name for name in entries if name.startswith(MDNS_RUNTIME_PREFIX))
    if not mdns.get("enabled"):
        if present or MDNS_MARKER in entries:
            fail("mDNS runtime is disabled but members are present")
        if expected_service_profile == "production":
            fail("production image is missing the shared discovery runtime")
        return False
    if (expected_mdns_runtime_manifest_sha256 is not None and
            mdns.get("manifest_sha256") != expected_mdns_runtime_manifest_sha256):
        fail("mDNS runtime manifest identity mismatch")
    for key in ("airplay_payload_dependency", "feature_payload_dependency"):
        if mdns.get(key) is not False:
            fail(f"mDNS runtime must not depend on another payload: {key}")
    if mdns.get("single_responder") is not True:
        fail("mDNS runtime must declare single-responder ownership")
    if mdns.get("contract") != contract["schema"]:
        fail("mDNS runtime contract identity mismatch")
    if mdns.get("runtime_dirs") != contract["runtime_dirs"]:
        fail("mDNS runtime directory contract mismatch")
    raw_files = mdns.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        fail("mDNS runtime inventory is missing")
    records = cast(dict[str, object], raw_files)
    required = {contract["loader"]}
    for category in MDNS_CONTRACT_CATEGORIES:
        required |= set(contract[category])
    missing = sorted(required - set(records))
    if missing:
        fail(f"mDNS runtime contract input missing: {missing[0]}")
    for relative in sorted(records):
        raw_record = records[relative]
        if not isinstance(raw_record, dict):
            fail(f"mDNS runtime record is malformed: {relative}")
        record = cast(dict[str, object], raw_record)
        digest = record.get("sha256")
        size = record.get("size")
        mode = record.get("mode")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                not isinstance(size, int) or mode not in ("0644", "0755")):
            fail(f"mDNS runtime record is invalid: {relative}")
        member = require_member(
            entries, MDNS_RUNTIME_PREFIX + relative, digest,
            0o755 if mode == "0755" else 0o644,
        )
        if len(member.data) != size:
            fail(f"mDNS runtime member size changed: {relative}")
        info = elf_info(member.data)
        if relative == contract["loader"]:
            if info is None or info[:3] != (1, 40, 0x05000400) or info[3] is not None:
                fail("mDNS runtime loader ELF contract changed")
            continue
        if info is None:
            continue
        if info[:3] != (1, 40, 0x05000400):
            fail(f"mDNS runtime member is not ARM32 hard-float: {relative}")
        if relative in contract["executables"] and (
                info[3] != contract["abi"]["interpreter"] or not info[5]):
            fail(f"mDNS runtime executable ELF contract changed: {relative}")

    raw_marker = entries.get(MDNS_MARKER)
    if raw_marker is None or not stat.S_ISREG(raw_marker.mode):
        fail("mDNS runtime marker is missing")
    if sha256(raw_marker.data) != mdns.get("marker_sha256"):
        fail("mDNS runtime marker hash mismatch")
    try:
        marker = json.loads(raw_marker.data)
    except json.JSONDecodeError:
        fail("mDNS runtime marker is not JSON")
    if (not isinstance(marker, dict) or marker.get("schema") != MDNS_MARKER_SCHEMA or
            marker.get("contract") != contract["schema"] or
            marker.get("readiness") != "presence-is-not-readiness" or
            marker.get("manifest_sha256") != mdns.get("manifest_sha256") or
            marker.get("runtime_dirs") != mdns.get("runtime_dirs") or
            marker.get("init") != "/" + MDNS_INIT or
            mdns.get("init") != "/" + MDNS_INIT):
        fail("mDNS runtime marker contract mismatch")
    init_entry = entries.get(MDNS_INIT)
    if (init_entry is None or not stat.S_ISREG(init_entry.mode) or
            stat.S_IMODE(init_entry.mode) != 0o755):
        fail("mDNS init entry is missing from the image")
    return True


def validate_connectivity_runtime_symlinks(
        entries: dict[str, Entry], record: object) -> None:
    if not isinstance(record, dict):
        fail("connectivity runtime symlink manifest is malformed")
    assert isinstance(record, dict)
    if record.get("symlinks") != CONNECTIVITY_RUNTIME_SYMLINKS:
        fail("connectivity runtime symlink manifest mismatch")
    for name, target in CONNECTIVITY_RUNTIME_SYMLINKS.items():
        entry = entries.get(name)
        if (
            entry is None
            or not stat.S_ISLNK(entry.mode)
            or stat.S_IMODE(entry.mode) != 0o777
            or entry.data != target.encode()
        ):
            fail(f"connectivity runtime symlink contract mismatch for {name}")
        resolved = resolve_relative_symlink(name, target)
        resolved_entry = entries.get(resolved)
        if resolved_entry is None or not stat.S_ISDIR(resolved_entry.mode):
            fail(f"connectivity runtime symlink dangles: {name} -> {target}")


def validate_connectivity(entries: dict[str, Entry], manifest: dict[str, object],
                          schema_version: int) -> bool:
    record = manifest.get("connectivity", {"enabled": False})
    if not isinstance(record, dict) or not isinstance(record.get("enabled"), bool):
        fail("connectivity manifest record is malformed")
    assert isinstance(record, dict)

    embedded_vendor = sorted(
        name for name in entries
        if name in {
            *(f"lib/firmware/{asset}" for asset in CONNECTIVITY_ASSET_REQUIREMENTS),
            "lib/firmware/WIFI_RAM_CODE",
        }
    )
    if embedded_vendor:
        fail(f"vendor firmware is embedded in the initramfs: {embedded_vendor}")

    if not record["enabled"]:
        runtime_members = set(CONNECTIVITY_HELPERS) | set(CONNECTIVITY_RUNTIME_SYMLINKS)
        unexpected = sorted(name for name in runtime_members if name in entries)
        if unexpected:
            fail(f"connectivity bundle is disabled but members are present: {unexpected}")
        if schema_version == 2:
            expected_disabled = {
                "id": CONNECTIVITY_BUNDLE_ID,
                "enabled": False,
                "activation": "manual-gates-only",
                "autostart": False,
                "files": {},
                "helpers": {},
                "symlinks": {},
            }
            if record != expected_disabled:
                fail("disabled connectivity manifest record changed")
        return False

    if schema_version != 2:
        fail("enabled connectivity bundle requires manifest schema 2")
    expected_policy = {
        "id": CONNECTIVITY_BUNDLE_ID,
        "activation": "manual-gates-only",
        "autostart": False,
        "vendor_delivery": "owner-device-local-extraction",
        "source_partition": "system_a-read-only",
        "embedded_vendor_file_count": 0,
        "required_vendor_file_count": len(CONNECTIVITY_ASSET_REQUIREMENTS),
        "required_vendor_bytes": sum(
            int(record["size"])
            for record in CONNECTIVITY_ASSET_REQUIREMENTS.values()
        ),
        "helper_count": len(CONNECTIVITY_HELPERS),
        "payload_bytes": sum(size for size, _digest in CONNECTIVITY_HELPERS.values()),
        "files": {},
        "symlinks": CONNECTIVITY_RUNTIME_SYMLINKS,
    }
    for key, value in expected_policy.items():
        if record.get(key) != value:
            fail(f"connectivity local-extraction policy changed for {key}")
    validate_connectivity_runtime_symlinks(entries, record)

    expected_requirements: dict[str, object] = {}
    expected_spec_lines = []
    for target_name, specification in CONNECTIVITY_ASSET_REQUIREMENTS.items():
        source_name = str(specification["source"])
        expected_size = int(specification["size"])
        expected_hash = str(specification["sha256"])
        expected_spec_lines.append(
            f"{expected_hash}|{expected_size}|{source_name}|{target_name}\n"
        )
        expected_requirements[target_name] = {
            "source": source_name,
            "sha256": expected_hash,
            "size": expected_size,
            "mode": "0600",
            "persistent_path": f"/data/libreecho/vendor/{CONNECTIVITY_BUNDLE_ID}/{target_name}",
            "runtime_path": f"/lib/firmware/{target_name}",
        }
    if not strictly_equal(record.get("required_vendor_assets"), expected_requirements):
        fail("connectivity local vendor requirements changed")

    forbidden = sorted(
        name for name in entries
        if name == "system/bin/linker" or name.startswith("system/lib/") or
           name.startswith("system/vendor/")
    )
    if forbidden:
        fail(f"stock Android connectivity userspace remains embedded: {forbidden}")

    expected_spec = "".join(expected_spec_lines).encode()
    spec_name = f"etc/libreecho/vendor-assets/{CONNECTIVITY_BUNDLE_ID}.tsv"
    spec_member = require_member(entries, spec_name, sha256(expected_spec), 0o644)
    if spec_member.data != expected_spec:
        fail("local vendor requirements manifest content changed")
    if record.get("requirements_manifest") != {
        "path": "/" + spec_name,
        "sha256": sha256(expected_spec),
        "size": len(expected_spec),
        "mode": "0644",
    }:
        fail("local vendor requirements manifest record changed")

    raw_importer = record.get("importer")
    if not isinstance(raw_importer, dict):
        fail("local vendor importer manifest record is missing")
    importer = require_member(
        entries, "usr/local/sbin/libreecho-vendor-import",
        CONNECTIVITY_IMPORTER_SHA256, 0o755,
    )
    if raw_importer != {
        "path": "/usr/local/sbin/libreecho-vendor-import",
        "sha256": CONNECTIVITY_IMPORTER_SHA256,
        "size": len(importer.data),
        "mode": "0755",
    }:
        fail("local vendor importer manifest record changed")

    helper_records = record.get("helpers")
    if not isinstance(helper_records, dict) or set(helper_records) != set(CONNECTIVITY_HELPERS):
        fail("connectivity helper manifest is incomplete")
    for name, (expected_size, expected_hash) in CONNECTIVITY_HELPERS.items():
        entry = require_member(entries, name, expected_hash, 0o755)
        if len(entry.data) != expected_size:
            fail(f"connectivity helper size mismatch for {name}")
        info = elf_info(entry.data)
        if info != (1, 40, 0x05000400, None, (), False):
            fail(f"connectivity helper is not static ARM32 hard-float: {name}: {info}")
        if helper_records.get(name) != {
            "sha256": expected_hash,
            "size": expected_size,
            "mode": "0755",
            "elf": {
                "class": 1,
                "machine": 40,
                "flags": "0x05000400",
                "interpreter": None,
                "needed": [],
                "dynamic": False,
            },
        }:
            fail(f"connectivity helper manifest record mismatch for {name}")

    return True


BOOT_HTTPS_CA_SHA256 = "c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b"


def validate_boot_https_transport(entries: dict[str, Entry], manifest: dict[str, object],
                                 expected_payload_sha256: str | None) -> None:
    # Independently enforce paths, ABI, mode, CA pin and closure; not presence-only.
    sources = {
        "usr/bin/curl": "usr/local/libexec/libreecho-curl",
        "etc/ssl/certs/ca-certificates.crt": "usr/local/share/libreecho/cacert.pem",
        "usr/local/share/licenses/curl/COPYING": "usr/local/share/licenses/curl/COPYING",
        "usr/local/share/licenses/ca-certificates/copyright": "usr/local/share/licenses/ca-certificates/copyright",
    }
    for name in ("THIRD_PARTY_NOTICES.txt", "OpenSSL-copyright", "glibc-copyright",
                 "gcc-runtime-copyright", "LGPL-2.1.txt", "GPL-3.0.txt"):
        path = "usr/local/share/licenses/libreecho-assistant/" + name
        sources[path] = path
    record = manifest.get("boot_https_transport")
    if (not isinstance(record, dict) or
            record.get("schema") != "libreecho-boot-https-transport/v1" or
            record.get("payload_dependency") is not False or
            record.get("userdata_dependency") is not False or
            record.get("client") != "/usr/bin/curl" or
            record.get("ca") != "/etc/ssl/certs/ca-certificates.crt" or
            record.get("curl_source_sha256") != "aa1b66a70eace83dc624508745646c08ae561de512ab403adffb93ac87fc72e6"):
        fail("mandatory boot HTTPS transport manifest is missing or invalid")
    assert isinstance(record, dict)
    if (expected_payload_sha256 is None or
            not re.fullmatch(r"[0-9a-f]{64}", expected_payload_sha256) or
            record.get("source_payload_sha256") != expected_payload_sha256 or
            not re.fullmatch(r"[0-9a-f]{64}", str(record.get("source_manifest_sha256", "")))):
        fail("boot HTTPS transport source identity is not trusted")
    if record.get("capabilities") != {
            "curl": "8.21.0", "tls": "OpenSSL/3.0.13", "protocols": ["http", "https"]}:
        fail("boot HTTPS client capability record is missing or invalid")
    files = record.get("files")
    if not isinstance(files, dict) or set(files) != set(sources):
        fail("boot HTTPS transport closure is incomplete")
    assert isinstance(files, dict)
    for name, source in sources.items():
        item = files[name]
        mode = 0o755 if name == "usr/bin/curl" else 0o644
        if (not isinstance(item, dict) or
                set(item) != {"source_member", "sha256", "size", "mode"} or
                item.get("source_member") != source or item.get("mode") != f"{mode:04o}" or
                type(item.get("size")) is not int or item["size"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))):
            fail(f"boot HTTPS transport record is invalid: {name}")
        assert isinstance(item, dict)
        member = require_member(entries, name, item["sha256"], mode)
        if len(member.data) != item["size"]:
            fail(f"boot HTTPS transport member size mismatch: {name}")
        if name == "usr/bin/curl" and elf_info(member.data) != (1, 40, 0x05000400, None, (), False):
            fail("boot HTTPS curl is not static ARM32 hard-float")
        if name.startswith("etc/ssl/") and sha256(member.data) != BOOT_HTTPS_CA_SHA256:
            fail("boot HTTPS CA identity changed")
    assistant = manifest.get("assistant", {})
    if isinstance(assistant, dict) and assistant.get("enabled"):
        payload = assistant.get("payload", {})
        if isinstance(payload, dict) and payload.get("sha256") == expected_payload_sha256:
            for name, source in sources.items():
                originals = payload.get("files")
                if not isinstance(originals, dict) or not isinstance(originals.get(source), dict):
                    fail(f"boot HTTPS source member is missing: {source}")
                assert isinstance(originals, dict)
                original = originals[source]
                if any(original.get(key) != files[name][key] for key in ("sha256", "size", "mode")):
                    fail(f"boot HTTPS transport differs from verified assistant input: {name}")


def validate_target_identity(entries: dict[str, Entry], manifest: dict[str, object],
                             target: str, digest: str | None = None) -> None:
    get_target(target)
    if manifest.get("board") != target or manifest.get("ota", {}).get("board") != target:
        fail("image target manifest mismatch")
    recorded_digest = manifest.get("target_descriptor_sha256")
    if digest is not None and recorded_digest != digest:
        fail("image target descriptor digest mismatch")
    # Validate a descriptor supplied by Product as a canonical digest even when
    # the caller did not request one; it must agree bytewise with the image.
    require_member(entries, "etc/libreecho/target",
                   sha256(identity_bytes(target, recorded_digest)), 0o644)
    # The speaker policy must be exactly the table rendering for this target.
    profile_hash = sha256(audio_profile_bytes(target))
    require_member(entries, "etc/libreecho/audio-profile", profile_hash, 0o644)
    if manifest.get("audio_profile", {}).get("sha256") != profile_hash:
        fail("image audio profile manifest mismatch")


def validate_initramfs(ramdisk: bytes, manifest: dict[str, object],
                       schema_version: int,
                       expected_image_profile: str,
                       expected_service_profile: str,
                       expected_feature_policy: str,
                       expected_update_channel: str,
                       expected_busybox_sha256: str,
                       expected_loader_sha256: str,
                       expected_bootctl_sha256: str,
                       expected_update_verifier_sha256: str,
                       expected_ota_public_key_sha256: str,
                       expected_adbd_sha256: str,
                       expected_audio_probe_sha256: str | None,
                       expected_tinyplay_sha256: str | None,
                       expected_tinycap_sha256: str | None,
                       expected_tinymix_sha256: str | None,
                       expected_iwconfig_sha256: str | None,
                       expected_dropbear_sha256: str | None,
                       expected_dropbearkey_sha256: str | None,
                       expected_scp_sha256: str | None,
                       expected_ui_manifest_sha256: str | None,
                       expected_ui_commit: str | None,
                       expected_ui_diff_sha256: str | None,
                       expected_airplay_payload_sha256: str | None,
                       expected_airplay_payload_size: int | None,
                       expected_tts_payload_sha256: str | None,
                       expected_tts_payload_size: int | None,
                       expected_wakeword_payload_sha256: str | None,
                       expected_wakeword_payload_size: int | None,
                       expected_stt_payload_sha256: str | None,
                       expected_stt_payload_size: int | None,
                       expected_assistant_payload_sha256: str | None,
                       expected_assistant_payload_size: int | None,
                       expected_nqptp_sha256: str | None,
                       expected_shairport_sync_sha256: str | None,
                       expected_avahi_daemon_sha256: str | None,
                       expected_dbus_daemon_sha256: str | None,
                       expected_wpa_supplicant_sha256: str | None = None,
                       expected_mdns_runtime_manifest_sha256: str | None = None,
                       expected_target: str | None = None,
                       target_descriptor_sha256: str | None = None,
                       expected_boot_https_payload_sha256: str | None = None) -> bool:
    cpio = decompress_ramdisk(ramdisk)
    entries = parse_newc(cpio)
    validate_archive_tree(entries)
    validate_symlinks(entries)
    validate_boot_https_transport(
        entries, manifest, expected_boot_https_payload_sha256 or expected_assistant_payload_sha256,
    )
    validate_no_connectivity_autostart(entries)
    if expected_target is not None:
        validate_target_identity(entries, manifest, expected_target, target_descriptor_sha256)
    target = expected_target or "radar_puffin"
    if manifest.get("image_profile") != expected_image_profile:
        fail("image profile manifest mismatch")
    if manifest.get("service_profile") != expected_service_profile:
        fail("service profile manifest mismatch")
    if manifest.get("feature_policy") != expected_feature_policy:
        fail("feature policy manifest mismatch")
    if manifest.get("update_channel") != expected_update_channel:
        fail("update channel manifest mismatch")
    require_member(
        entries, "etc/libreecho/image-profile",
        sha256((expected_image_profile + "\n").encode()), 0o644,
    )
    require_member(
        entries, "etc/libreecho/service-profile",
        sha256((expected_service_profile + "\n").encode()), 0o644,
    )
    require_member(
        entries, "etc/libreecho/feature-policy",
        sha256((expected_feature_policy + "\n").encode()), 0o644,
    )
    require_member(
        entries, "etc/libreecho/update-channel",
        sha256((expected_update_channel + "\n").encode()), 0o644,
    )
    require_member(
        entries, "etc/libreecho/first-install-confirm",
        sha256(f"schema=1\nmode=first-install\nboard={target}\n".encode()), 0o644,
    )
    ota = manifest.get("ota")
    if not isinstance(ota, dict) or ota.get("format") != "libreecho-ota-v1":
        fail("OTA manifest record is missing or malformed")
    if ota.get("payload_slots") != {"a": "mmcblk0p10", "b": "mmcblk0p11"}:
        fail("OTA payload-slot mapping changed")
    if ota.get("wrapper_partitions") != ["mmcblk0p17", "mmcblk0p18"]:
        fail("Amonet wrapper partition denylist changed")
    for name, path, expected_hash, expected_interpreter in (
        ("bootctl", "usr/local/sbin/libreecho-bootctl", expected_bootctl_sha256,
         "/lib/ld-musl-armhf.so.1"),
        ("verifier", "usr/local/libexec/libreecho-update-verify",
         expected_update_verifier_sha256, None),
    ):
        member = require_member(entries, path, expected_hash, 0o755)
        info = elf_info(member.data)
        if info is None or info[:2] != (1, 40) or info[3] != expected_interpreter:
            fail(f"OTA {name} ELF interpreter contract changed")
        if name == "bootctl" and (info[4] != ("libc.musl-armv7.so.1",) or not info[5]):
            fail("OTA bootctl musl dependency contract changed")
        if name == "verifier" and (info[4] or info[5]):
            fail("OTA signature verifier is not static")
        record = ota.get("tools", {}).get(name, {})
        if record.get("sha256") != expected_hash or record.get("path") != "/" + path:
            fail(f"OTA {name} manifest identity mismatch")
    require_member(
        entries, "etc/libreecho/ota-public-key.hex",
        expected_ota_public_key_sha256, 0o644,
    )
    if ota.get("public_key_sha256") != expected_ota_public_key_sha256:
        fail("OTA public-key manifest identity mismatch")
    network = manifest.get("network", {"enabled": False})
    if not isinstance(network, dict) or not isinstance(network.get("enabled"), bool):
        fail("network manifest record is malformed")
    network = cast(dict[str, object], network)
    network_names = {"sbin/wpa_supplicant", "etc/wifi/wpa_supplicant.conf"}
    if network.get("enabled"):
        if network.get("activation") != "manual-single-shot-after-adb":
            fail("network activation policy changed")
        raw_wpa_record = network.get("wpa_supplicant")
        raw_profile_record = network.get("wifi_profile")
        if not isinstance(raw_wpa_record, dict) or not isinstance(raw_profile_record, dict):
            fail("network asset manifest is incomplete")
        wpa_record = cast(dict[str, object], raw_wpa_record)
        profile_record = cast(dict[str, object], raw_profile_record)
        wpa_hash_value = wpa_record.get("sha256")
        profile_hash_value = profile_record.get("sha256")
        if not isinstance(wpa_hash_value, str) or not isinstance(profile_hash_value, str):
            fail("network asset hashes are malformed")
        wpa_hash: str = cast(str, wpa_hash_value)
        profile_hash: str = cast(str, profile_hash_value)
        if (expected_wpa_supplicant_sha256 is not None and
                wpa_hash != expected_wpa_supplicant_sha256):
            fail("wpa_supplicant trusted identity mismatch")
        wpa = require_member(entries, "sbin/wpa_supplicant", wpa_hash, 0o755)
        if elf_info(wpa.data) != (1, 40, 0x05000400, None, (), False):
            fail("wpa_supplicant is not static ARM32 hard-float")
        source_record = wpa_record.get("source")
        required_source = {
            "binary_sha256", "binary_size", "build_epoch", "compiler", "config_path",
            "config_sha256", "crypto", "drivers", "kernel_uapi_sha256", "license",
            "libnl_license", "libnl_source_sha256", "libnl_source_url", "libnl_version",
            "source_sha256", "source_url", "static", "version",
        }
        if (not isinstance(source_record, dict) or set(source_record) != required_source or
                source_record.get("binary_sha256") != wpa_hash or
                source_record.get("binary_size") != len(wpa.data) or
                source_record.get("source_sha256") != WPA_SOURCE_SHA256 or
                source_record.get("source_url") != WPA_SOURCE_URL or
                source_record.get("license") != "BSD-3-Clause" or
                source_record.get("version") != WPA_SUPPLICANT_VERSION or
                source_record.get("static") is not True or
                source_record.get("drivers") != ["nl80211", "wext"] or
                source_record.get("libnl_version") != "3.11.0" or
                source_record.get("libnl_license") != "LGPL-2.1-only" or
                source_record.get("libnl_source_sha256") != LIBNL_SOURCE_SHA256 or
                source_record.get("libnl_source_url") != LIBNL_SOURCE_URL or
                not re.fullmatch(r"[0-9a-f]{64}", str(source_record.get("config_sha256", ""))) or
                not re.fullmatch(r"[0-9a-f]{64}", str(source_record.get("kernel_uapi_sha256", "")))):
            fail("wpa source provenance is missing or mismatched")
        profile = require_member(entries, "etc/wifi/wpa_supplicant.conf", profile_hash, 0o600)
        if b"CHANGE_ME" in profile.data:
            fail("configured network image contains the profile template")
        for required in (
            "sbin/libreecho-wifi",
            "etc/udhcpc.script",
            "etc/wifi/wpa_supplicant.conf.example",
        ):
            if required not in entries:
                fail(f"network stack member missing: {required}")
    else:
        unexpected = sorted(name for name in network_names if name in entries)
        if unexpected:
            fail(f"network stack is disabled but members are present: {unexpected}")
    validate_network_tools(entries, manifest, expected_iwconfig_sha256)
    validate_ui(
        entries, manifest, expected_ui_manifest_sha256,
        expected_ui_commit, expected_ui_diff_sha256,
    )
    tts = manifest.get("tts", {"enabled": False})
    if not isinstance(tts, dict) or not isinstance(tts.get("enabled"), bool):
        fail("TTS manifest record is malformed")
    if expected_tts_payload_sha256 is not None or expected_tts_payload_size is not None:
        payload = tts.get("payload")
        if (expected_tts_payload_sha256 is None or expected_tts_payload_size is None or
                not tts.get("enabled") or not tts.get("external_payload") or
                tts.get("voices") != ["southern-female", "northern-male"] or
                tts.get("default_voice") != "southern-female" or
                tts.get("threads") != 4 or tts.get("streaming") is not True or
                tts.get("in_process") is not True or
                tts.get("cpu_boost_during_synthesis") is not True or
                not isinstance(payload, dict) or payload.get("format") != "squashfs-lz4" or
                payload.get("filename") != "tts.squashfs" or
                payload.get("sha256") != expected_tts_payload_sha256 or
                payload.get("size") != expected_tts_payload_size):
            fail("external TTS payload manifest is incomplete or mismatched")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            fail("external TTS payload file manifest is missing")
        for required in (
            "usr/local/sbin/libreecho-ttsd",
            "usr/local/share/libreecho/tts/models/northern-male/model.onnx",
            "usr/local/share/libreecho/tts/models/northern-male/tokens.txt",
            "usr/local/share/libreecho/tts/models/southern-female/model.onnx",
            "usr/local/share/libreecho/tts/models/southern-female/tokens.txt",
        ):
            if required not in files:
                fail(f"external TTS payload member missing: {required}")
        for voice in ("northern-male", "southern-female"):
            prefix = f"usr/local/share/libreecho/tts/models/{voice}/espeak-ng-data/"
            if not any(str(relative).startswith(prefix) for relative in files):
                fail(f"external TTS payload lacks eSpeak data for {voice}")
        for required_notice in (
            "usr/local/share/licenses/libreecho-tts/THIRD_PARTY_NOTICES.md",
            "usr/local/share/licenses/libreecho-tts/NORTHERN-MALE-MODEL-CARD.md",
            "usr/local/share/licenses/libreecho-tts/SOUTHERN-FEMALE-MODEL-CARD.md",
            "usr/local/share/licenses/libreecho-tts/CC-BY-SA-4.0.txt",
            "usr/local/share/licenses/libreecho-tts/runtime/RUNTIME-NOTICES.txt",
            "usr/local/share/licenses/libreecho-tts/runtime/ONNX-Runtime-MIT.txt",
            "usr/local/share/licenses/libreecho-tts/runtime/sherpa-onnx-Apache-2.0.txt",
            "usr/local/share/licenses/libreecho-tts/runtime/SpeexDSP-COPYING.txt",
        ):
            if required_notice not in files:
                fail(f"external TTS payload notice missing: {required_notice}")
        for relative, record in files.items():
            if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                    "//" in relative or "/../" in f"/{relative}/" or
                    not isinstance(record, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
                fail(f"external TTS payload contains an unsafe file record: {relative!r}")
        if "usr/local/sbin/libreecho-ttsd" in entries:
            fail("external TTS daemon leaked into the boot ramdisk")
    elif tts.get("enabled"):
        fail("TTS manifest is enabled without an expected external payload")
    wakeword = manifest.get("wakeword", {"enabled": False})
    if (not isinstance(wakeword, dict) or
            not isinstance(wakeword.get("enabled"), bool)):
        fail("wakeword manifest record is malformed")
    if (expected_wakeword_payload_sha256 is not None or
            expected_wakeword_payload_size is not None):
        payload = wakeword.get("payload")
        if (expected_wakeword_payload_sha256 is None or
                expected_wakeword_payload_size is None or
                not wakeword.get("enabled") or
                not wakeword.get("external_payload") or
                wakeword.get("engine") != "openwakeword-onnx" or
                wakeword.get("wake_word") != "Alexa" or
                wakeword.get("development_model") is not True or
                wakeword.get("model_license") != "CC-BY-NC-SA-4.0" or
                wakeword.get("threads") != 2 or
                wakeword.get("sample_rate_hz") != 16000 or
                wakeword.get("block_samples") != 1280 or
                wakeword.get("continuous_model_input") is not True or
                not isinstance(payload, dict) or
                payload.get("format") != "squashfs-lz4" or
                payload.get("filename") != "wakeword.squashfs" or
                payload.get("sha256") != expected_wakeword_payload_sha256 or
                payload.get("size") != expected_wakeword_payload_size):
            fail("external wakeword payload manifest is incomplete or mismatched")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            fail("external wakeword payload file manifest is missing")
        for required in (
            "usr/local/sbin/libreecho-waked",
            "usr/local/share/libreecho/openwakeword/melspectrogram.onnx",
            "usr/local/share/libreecho/openwakeword/embedding_model.onnx",
            "usr/local/share/libreecho/openwakeword/alexa_v0.1.onnx",
            "usr/local/share/licenses/libreecho-openwakeword/MODEL-LICENSE.txt",
            "usr/local/share/licenses/libreecho-openwakeword/CC-BY-NC-SA-4.0.txt",
            "usr/local/share/licenses/libreecho-openwakeword/runtime/RUNTIME-NOTICES.txt",
            "usr/local/share/licenses/libreecho-openwakeword/runtime/ONNX-Runtime-MIT.txt",
            "usr/local/share/licenses/libreecho-openwakeword/runtime/SpeexDSP-COPYING.txt",
        ):
            if required not in files:
                fail(f"external wakeword payload member missing: {required}")
        for relative, record in files.items():
            if (not isinstance(relative, str) or not relative or
                    relative.startswith("/") or "//" in relative or
                    "/../" in f"/{relative}/" or not isinstance(record, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}",
                                     str(record.get("sha256", "")))):
                fail(
                    "external wakeword payload contains an unsafe file "
                    f"record: {relative!r}"
                )
        if "usr/local/sbin/libreecho-waked" in entries:
            fail("external wakeword daemon leaked into the boot ramdisk")
    elif wakeword.get("enabled"):
        fail("wakeword manifest is enabled without an expected external payload")
    stt = manifest.get("stt", {"enabled": False})
    if not isinstance(stt, dict) or not isinstance(stt.get("enabled"), bool):
        fail("STT manifest record is malformed")
    if expected_stt_payload_sha256 is not None or expected_stt_payload_size is not None:
        payload = stt.get("payload")
        if (expected_stt_payload_sha256 is None or
                expected_stt_payload_size is None or
                not stt.get("enabled") or not stt.get("external_payload") or
                stt.get("engine") != "sherpa-onnx-streaming-zipformer" or
                stt.get("language") != "en" or
                stt.get("quantization") != "int8" or
                stt.get("threads") != 2 or
                stt.get("sample_rate_hz") != 16000 or
                stt.get("endpoint_trailing_silence_ms") != 500 or
                stt.get("streaming") is not True or
                stt.get("model_license") != "Apache-2.0" or
                not isinstance(payload, dict) or
                payload.get("format") != "squashfs-lz4" or
                payload.get("filename") != "stt.squashfs" or
                payload.get("sha256") != expected_stt_payload_sha256 or
                payload.get("size") != expected_stt_payload_size):
            fail("external STT payload manifest is incomplete or mismatched")
        files = payload.get("files")
        expected_stt_files = {
            "usr/local/sbin/libreecho-sttd": None,
            "usr/local/share/libreecho/stt/encoder-epoch-99-avg-1.int8.onnx":
                "3810755ce7c3ab26b42a8bcf39d191308fa27fb0f53358823ba46141d03b7eb3",
            "usr/local/share/libreecho/stt/decoder-epoch-99-avg-1.int8.onnx":
                "21e2a2acd961b3ac72f55be2f10f1a285e1b0b0ba010d7c0b6eab141411b163c",
            "usr/local/share/libreecho/stt/joiner-epoch-99-avg-1.int8.onnx":
                "e085d73b593cf9b0707f370dbd656d58327d3fe36d80d849202ef81df02cb01e",
            "usr/local/share/libreecho/stt/tokens.txt":
                "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
            "usr/local/share/licenses/libreecho-stt-model/MODEL-LICENSE.md":
                "505f6b0e8a39f066a0794c4fb0b5689533d3bcd9d1dc5e5f47ccffeef1af9877",
            "usr/local/share/licenses/libreecho-stt-runtime/RUNTIME-NOTICES.txt": None,
            "usr/local/share/licenses/libreecho-stt-runtime/ONNX-Runtime-MIT.txt": None,
            "usr/local/share/licenses/libreecho-stt-runtime/sherpa-onnx-Apache-2.0.txt": None,
            "usr/local/share/licenses/libreecho-stt-runtime/SpeexDSP-COPYING.txt": None,
        }
        if not isinstance(files, dict) or not files:
            fail("external STT payload file manifest is missing")
        for required, expected_hash in expected_stt_files.items():
            record = files.get(required)
            if not isinstance(record, dict):
                fail(f"external STT payload member missing: {required}")
            if expected_hash is not None and record.get("sha256") != expected_hash:
                fail(f"external STT payload member hash changed: {required}")
        for relative, record in files.items():
            if (not isinstance(relative, str) or not relative or
                    relative.startswith("/") or "//" in relative or
                    "/../" in f"/{relative}/" or not isinstance(record, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}",
                                     str(record.get("sha256", "")))):
                fail(f"external STT payload has unsafe file record: {relative!r}")
        if "usr/local/sbin/libreecho-sttd" in entries:
            fail("external STT daemon leaked into the boot ramdisk")
    elif stt.get("enabled"):
        fail("STT manifest is enabled without an expected external payload")
    assistant = manifest.get("assistant", {"enabled": False})
    if (not isinstance(assistant, dict) or
            not isinstance(assistant.get("enabled"), bool)):
        fail("assistant manifest record is malformed")
    if (expected_assistant_payload_sha256 is not None or
            expected_assistant_payload_size is not None):
        payload = assistant.get("payload")
        if (expected_assistant_payload_sha256 is None or
                expected_assistant_payload_size is None or
                not assistant.get("enabled") or
                not assistant.get("external_payload") or
                assistant.get("provider") != "openai-codex" or
                assistant.get("provider_neutral_boundary") is not True or
                assistant.get("subscription_device_auth") is not True or
                assistant.get("metered_api_key_auth") is not False or
                assistant.get("text_streaming") is not True or
                assistant.get("sentence_streaming_to_tts") is not True or
                assistant.get("latency_target_ms") != 3000 or
                assistant.get("credential_storage") != "private-persistent-0600" or
                not isinstance(payload, dict) or
                payload.get("format") != "squashfs-lz4" or
                payload.get("filename") != "assistant.squashfs" or
                payload.get("sha256") != expected_assistant_payload_sha256 or
                payload.get("size") != expected_assistant_payload_size):
            fail(
                "external assistant payload manifest is incomplete or mismatched"
            )
        files = payload.get("files")
        expected_assistant_files = {
            "usr/local/sbin/libreecho-agentd": None,
            "usr/local/libexec/libreecho-curl": None,
            "usr/local/share/libreecho/cacert.pem":
                "c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b",
            "usr/local/share/licenses/curl/COPYING": None,
            "usr/local/share/licenses/ca-certificates/copyright": None,
            "usr/local/share/licenses/libreecho-assistant/THIRD_PARTY_NOTICES.txt": None,
            "usr/local/share/licenses/libreecho-assistant/OpenSSL-copyright": None,
            "usr/local/share/licenses/libreecho-assistant/glibc-copyright": None,
            "usr/local/share/licenses/libreecho-assistant/gcc-runtime-copyright": None,
        }
        if not isinstance(files, dict) or not files:
            fail("external assistant payload file manifest is missing")
        for required, expected_hash in expected_assistant_files.items():
            record = files.get(required)
            if not isinstance(record, dict):
                fail(f"external assistant payload member missing: {required}")
            if expected_hash is not None and record.get("sha256") != expected_hash:
                fail(f"external assistant payload member hash changed: {required}")
        for relative, record in files.items():
            if (not isinstance(relative, str) or not relative or
                    relative.startswith("/") or "//" in relative or
                    "/../" in f"/{relative}/" or not isinstance(record, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}",
                                     str(record.get("sha256", "")))):
                fail(
                    "external assistant payload has unsafe file record: "
                    f"{relative!r}"
                )
            if ("credential" in relative.lower() or
                    "openai-codex.json" in relative.lower() or
                    "api-key" in relative.lower()):
                fail("assistant payload contains credential material")
        if "usr/local/sbin/libreecho-agentd" in entries:
            fail("external assistant daemon leaked into the boot ramdisk")
        if any(
                "openai-codex.json" in name.lower() or "api-key" in name.lower()
                for name in entries):
            fail("assistant credentials leaked into the boot ramdisk")
    elif assistant.get("enabled"):
        fail(
            "assistant manifest is enabled without an expected external payload"
        )
    airplay = manifest.get("airplay", {"enabled": False})
    if not isinstance(airplay, dict) or not isinstance(airplay.get("enabled"), bool):
        fail("AirPlay manifest record is malformed")
    airplay = cast(dict[str, object], airplay)
    airplay_names = set(AIRPLAY_BINARY_NAMES)
    runtime = airplay.get("runtime", {})
    if isinstance(runtime, dict):
        airplay_names.update(str(name) for name in runtime)
    expected_airplay = (
        expected_nqptp_sha256, expected_shairport_sync_sha256,
        expected_avahi_daemon_sha256, expected_dbus_daemon_sha256,
    )
    if airplay.get("external_payload"):
        payload = airplay.get("payload")
        if (not isinstance(payload, dict) or payload.get("format") != "squashfs-lz4" or
                payload.get("filename") != "airplay2.squashfs" or
                not isinstance(payload.get("sha256"), str) or
                not isinstance(payload.get("size"), int) or
                expected_airplay_payload_sha256 is None or
                expected_airplay_payload_size is None or
                payload.get("sha256") != expected_airplay_payload_sha256 or
                payload.get("size") != expected_airplay_payload_size):
            fail("external AirPlay payload manifest is incomplete or mismatched")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            fail("external AirPlay payload file manifest is missing")
        for required in (
            "usr/local/sbin/libreecho-airplay-audio",
            "usr/local/sbin/libreecho-audio-engine",
            "usr/local/sbin/shairport-sync",
            "etc/libreecho/airplay2.conf",
            "usr/local/share/licenses/libreecho-airplay/COMPONENTS.tsv",
        ):
            if required not in files:
                fail(f"external AirPlay payload member missing: {required}")
        if not any(str(relative).startswith(
                "usr/local/share/licenses/libreecho-airplay/debian/") and
                str(relative).endswith("/copyright") for relative in files):
            fail("external AirPlay Debian copyright closure is missing")
        for component in ("nqptp", "shairport-sync", "ffmpeg", "tinyalsa"):
            prefix = f"usr/local/share/licenses/libreecho-airplay/source/{component}/"
            if not any(str(relative).startswith(prefix) for relative in files):
                fail(f"external AirPlay source license closure missing: {component}")
        for relative, record in files.items():
            if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                    "//" in relative or "/../" in f"/{relative}/" or
                    not isinstance(record, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
                fail(f"external AirPlay payload contains an unsafe file record: {relative!r}")
        unexpected_external = sorted(
            name for name in entries
            if name in AIRPLAY_BINARY_NAMES or name.startswith("usr/lib/") and ".so." in name or
               name == "lib/ld-linux-armhf.so.3" or name.startswith("etc/avahi/") or
               name.startswith("etc/dbus-1/")
        )
        if unexpected_external:
            fail(f"external AirPlay runtime leaked into boot ramdisk: {unexpected_external}")
    elif any(value is not None for value in expected_airplay):
        if not all(value is not None for value in expected_airplay):
            fail("AirPlay asset identities are incomplete")
        if not airplay.get("enabled"):
            fail("AirPlay assets are expected but the AirPlay manifest is disabled")
        nqptp = require_member(entries, "usr/local/sbin/nqptp", expected_nqptp_sha256, 0o755)
        if elf_info(nqptp.data) != (1, 40, 0x05000400, None, (), False):
            fail("NQPTP is not static ARM32 hard-float")
        dynamic_members = {
            "usr/local/sbin/shairport-sync": expected_shairport_sync_sha256,
            "usr/local/sbin/avahi-daemon": expected_avahi_daemon_sha256,
            "usr/local/sbin/dbus-daemon": expected_dbus_daemon_sha256,
        }
        dynamic_infos: dict[str, tuple[int, int, int, str | None, tuple[str, ...], bool]] = {}
        for name, expected_hash in dynamic_members.items():
            member = require_member(entries, name, expected_hash, 0o755)
            info = elf_info(member.data)
            if (info is None or info[:2] != (1, 40) or info[2] != 0x05000400 or
                    info[3] != "/lib/ld-linux-armhf.so.3" or not info[5]):
                fail(f"AirPlay daemon is not a dynamic ARMHF executable: {name}")
            dynamic_infos[name] = info
        shairport_info = dynamic_infos["usr/local/sbin/shairport-sync"]
        nqptp_record = airplay.get("nqptp")
        shairport_record = airplay.get("shairport_sync")
        if not isinstance(nqptp_record, dict) or not isinstance(shairport_record, dict):
            fail("AirPlay binary manifest records are incomplete")
        if nqptp_record.get("sha256") != expected_nqptp_sha256:
            fail("NQPTP manifest hash mismatch")
        if shairport_record.get("sha256") != expected_shairport_sync_sha256:
            fail("Shairport Sync manifest hash mismatch")
        if nqptp_record.get("elf", {}).get("dynamic") is not False:
            fail("NQPTP manifest incorrectly marks the binary as dynamic")
        if shairport_record.get("elf", {}).get("needed") != list(shairport_info[4]):
            fail("Shairport Sync dependency manifest mismatch")
        for key, info in dynamic_infos.items():
            manifest_key = {
                "usr/local/sbin/shairport-sync": "shairport_sync",
                "usr/local/sbin/avahi-daemon": "avahi_daemon",
                "usr/local/sbin/dbus-daemon": "dbus_daemon",
            }[key]
            record = airplay.get(manifest_key)
            if not isinstance(record, dict) or record.get("elf", {}).get("needed") != list(info[4]):
                fail(f"AirPlay daemon dependency manifest mismatch: {key}")
        if not isinstance(runtime, dict) or "lib/ld-linux-armhf.so.3" not in runtime:
            fail("AirPlay runtime manifest lacks the glibc loader")
        runtime_names = set(runtime)
        for relative, raw_record in runtime.items():
            if (not isinstance(relative, str) or
                    (relative != "lib/ld-linux-armhf.so.3" and
                     (not relative.startswith("usr/lib/") or ".so." not in relative))):
                fail("AirPlay runtime manifest contains an unsafe path")
            if not isinstance(raw_record, dict):
                fail(f"AirPlay runtime manifest record is malformed: {relative}")
            record = cast(dict[str, object], raw_record)
            config = relative.startswith("etc/")
            runtime_member = require_member(entries, relative, record.get("sha256"), 0o644 if config else 0o755)
            if config:
                if set(record) != {"sha256", "size", "mode"} or record.get("mode") != "0644":
                    fail(f"AirPlay runtime configuration record is malformed: {relative}")
                continue
            info = elf_info(runtime_member.data)
            if info is None or info[:2] != (1, 40) or info[2] != 0x05000400:
                fail(f"AirPlay runtime is not ARMHF: {relative}")
            if record.get("needed") is not None:
                fail("AirPlay runtime records must contain an ELF sub-record")
            raw_elf = record.get("elf")
            if not isinstance(raw_elf, dict):
                fail(f"AirPlay runtime ELF record is missing: {relative}")
            if (raw_elf.get("interpreter"), raw_elf.get("needed"), raw_elf.get("dynamic")) != (
                    info[3], list(info[4]), info[5]):
                fail(f"AirPlay runtime ELF record mismatch: {relative}")
        available_names = {PurePosixPath(name).name for name in runtime_names}
        needed = set().union(*(set(info[4]) for info in dynamic_infos.values()))
        if not needed.issubset(available_names):
            fail("AirPlay runtime closure does not cover Shairport Sync dependencies")
        unexpected_airplay = sorted(
            name for name in entries
            if name in AIRPLAY_BINARY_NAMES or name.startswith("usr/lib/") and ".so." in name or
               name == "lib/ld-linux-armhf.so.3" or name.startswith("etc/avahi/") or
               name.startswith("etc/dbus-1/")
        )
        if set(unexpected_airplay) != airplay_names:
            fail("AirPlay runtime members do not match the manifest")
    else:
        if airplay.get("enabled") or any(
            name in entries for name in AIRPLAY_BINARY_NAMES
        ):
            fail("AirPlay assets are present without expected identities")
    audio = manifest.get("audio", {"enabled": False})
    if not isinstance(audio, dict) or not isinstance(audio.get("enabled"), bool):
        fail("audio manifest record is malformed")
    audio = cast(dict[str, object], audio)
    audio_names = {
        "sbin/audio_probe", "sbin/tinyplay", "sbin/tinycap", "sbin/tinymix",
    }
    expected_audio = (
        expected_audio_probe_sha256,
        expected_tinyplay_sha256,
        expected_tinycap_sha256,
        expected_tinymix_sha256,
    )
    if any(value is not None for value in expected_audio):
        if not all(value is not None for value in expected_audio):
            fail("audio asset identities are incomplete")
        if not audio.get("enabled"):
            fail("audio assets are expected but audio manifest is disabled")
        raw_probe = audio.get("probe")
        if not isinstance(raw_probe, dict):
            fail("audio probe manifest record is incomplete")
        probe_record = cast(dict[str, object], raw_probe)
        if probe_record.get("sha256") != expected_audio_probe_sha256:
            fail("audio probe manifest hash mismatch")
        probe_path = probe_record.get("path")
        if not isinstance(probe_path, str) or not Path(probe_path).is_absolute():
            fail("audio probe manifest path is not absolute")
        probe = require_member(entries, "sbin/audio_probe", expected_audio_probe_sha256, 0o755)
        if elf_info(probe.data) != (1, 40, 0x05000400, None, (), False):
            fail("audio probe is not static ARM32 hard-float")
        if probe_record != {
            "path": probe_path,
            "sha256": expected_audio_probe_sha256,
            "size": len(probe.data),
            "mode": "0755",
            "elf": {
                "class": 1,
                "machine": 40,
                "flags": "0x05000400",
                "interpreter": None,
                "needed": [],
                "dynamic": False,
            },
        }:
            fail("audio probe manifest record mismatch")
        raw_tools = audio.get("tools")
        if not isinstance(raw_tools, dict):
            fail("audio tool manifest record is incomplete")
        tools = cast(dict[str, object], raw_tools)
        for name, expected_hash in (
            ("tinyplay", expected_tinyplay_sha256),
            ("tinycap", expected_tinycap_sha256),
            ("tinymix", expected_tinymix_sha256),
        ):
            if not isinstance(expected_hash, str):
                fail(f"{name} identity is missing")
            raw_tool = tools.get(name)
            if not isinstance(raw_tool, dict):
                fail(f"{name} manifest record is incomplete")
            tool_record = cast(dict[str, object], raw_tool)
            if tool_record.get("sha256") != expected_hash:
                fail(f"{name} manifest hash mismatch")
            tool_path = tool_record.get("path")
            if not isinstance(tool_path, str) or not Path(tool_path).is_absolute():
                fail(f"{name} manifest path is not absolute")
            tool = require_member(entries, f"sbin/{name}", expected_hash, 0o755)
            if elf_info(tool.data) != (1, 40, 0x05000400, None, (), False):
                fail(f"{name} is not static ARM32 hard-float")
            if tool_record != {
                "path": tool_path,
                "sha256": expected_hash,
                "size": len(tool.data),
                "mode": "0755",
                "elf": {
                    "class": 1,
                    "machine": 40,
                    "flags": "0x05000400",
                    "interpreter": None,
                    "needed": [],
                    "dynamic": False,
                },
            }:
                fail(f"{name} manifest record mismatch")
    elif audio.get("enabled") or sorted(name for name in audio_names if name in entries):
        fail("audio assets are enabled without expected identities")
    if sha256(cpio) != manifest["initramfs"]["cpio_sha256"]:
        fail("manifest cpio hash mismatch")
    if any(entry.uid or entry.gid or entry.mtime for entry in entries.values()):
        fail("initramfs ownership or mtime is not normalized")

    init = require_member(entries, "init", INIT_SHA256, 0o755)
    adbd = require_member(entries, "sbin/adbd", expected_adbd_sha256, 0o750)
    adbd_record = manifest.get("adbd")
    if not isinstance(adbd_record, dict) or adbd_record != {
        "path": "/sbin/adbd",
        "sha256": expected_adbd_sha256,
        "size": len(adbd.data),
        "mode": "0750",
        "source": adbd_record.get("source") if isinstance(adbd_record, dict) else None,
        "transport_policy": adbd_record.get("transport_policy")
        if isinstance(adbd_record, dict) else None,
    }:
        fail("adbd manifest record mismatch")
    source_record = adbd_record["source"]
    expected_transport = (
        ("usb-functionfs-and-tcp", True, 5555) if expected_update_channel == "dev"
        else ("usb-functionfs-only", False, 0)
    )
    if (
        not isinstance(source_record, dict)
        or source_record.get("source_license") != "Apache-2.0"
        or source_record.get("transport") != expected_transport[0]
        or source_record.get("tcp_listener") is not expected_transport[1]
        or type(source_record.get("tcp_port")) is not int
        or source_record["tcp_port"] != expected_transport[2]
        or not isinstance(source_record.get("kernel_headers"), str)
        or not source_record.get("kernel_headers")
        or not re.fullmatch(r"[0-9a-f]{40}", str(source_record.get("source_commit", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(source_record.get("patch_sha256", "")))
    ):
        fail("adbd source provenance or transport policy is invalid")
    # The image must carry the transport policy init reports, and it must agree
    # with the validated source record: a build that is USB-only must not be
    # reported as TCP-capable, and the staged file must bind to this binary.
    policy_record = adbd_record["transport_policy"]
    expected_policy = (
        f"schema=1\n"
        f"transport={source_record['transport']}\n"
        f"tcp_listener={'true' if source_record['tcp_listener'] else 'false'}\n"
        f"tcp_port={source_record['tcp_port']}\n"
        f"binary_sha256={expected_adbd_sha256}\n"
    ).encode()
    if not isinstance(policy_record, dict) or policy_record != {
        "path": "/etc/libreecho/adb-transport",
        "transport": source_record["transport"],
        "tcp_listener": source_record["tcp_listener"],
        "tcp_port": source_record["tcp_port"],
        "binary_sha256": expected_adbd_sha256,
    }:
        fail("adbd transport policy record mismatch")
    policy_member = require_member(
        entries, "etc/libreecho/adb-transport", sha256(expected_policy), 0o644
    )
    if policy_member.data != expected_policy:
        fail("staged adbd transport policy does not match the validated source")
    if "stock_userspace" in manifest:
        fail("stock userspace manifest entry is forbidden")
    busybox = require_member(entries, "bin/busybox", expected_busybox_sha256, 0o755)
    loader = require_member(entries, "lib/ld-musl-armhf.so.1", expected_loader_sha256, 0o755)
    for name, member, expected_interpreter in (
        ("sbin/adbd", adbd, None),
        ("bin/busybox", busybox, "/lib/ld-musl-armhf.so.1"),
        ("lib/ld-musl-armhf.so.1", loader, None),
    ):
        info = elf_info(member.data)
        if info is None or info[:2] != (1, 40) or info[3] != expected_interpreter:
            fail(f"ELF contract mismatch for {name}: {info}")
    if b"libc.musl-armv7.so.1\0" not in busybox.data:
        fail("BusyBox musl dependency is missing")

    symlinks = {
        "lib/libc.musl-armv7.so.1": b"ld-musl-armhf.so.1",
        "sbin/sh": b"../bin/busybox",
        "sbin/ueventd": b"../init",
        "sbin/watchdogd": b"../init",
        "system/bin/sh": b"../../bin/busybox",
    }
    for name, link_target in symlinks.items():
        entry = entries.get(name)
        if entry is None or not stat.S_ISLNK(entry.mode) or entry.data != link_target:
            fail(f"symlink contract mismatch for {name}")

    required_applets = (
        "cat", "dd", "dmesg", "hexdump", "ifconfig", "insmod", "ip", "ls",
        "mknod", "mount", "rmmod", "sh", "stat", "sync", "udhcpc",
    )
    for applet in required_applets:
        entry = entries.get("bin/" + applet)
        if entry is None or not stat.S_ISLNK(entry.mode) or entry.data != b"busybox":
            fail(f"BusyBox applet link is missing or unsafe: {applet}")
    applets = manifest.get("busybox_applets", {})
    if applets.get("count", 0) < 250 or not set(required_applets).issubset(applets.get("names", [])):
        fail("BusyBox applet manifest is incomplete")
    busybox_record = manifest.get("busybox")
    if not isinstance(busybox_record, dict) or busybox_record.get("sha256") != expected_busybox_sha256:
        fail("BusyBox manifest identity mismatch")
    loader_record = manifest.get("musl_loader")
    if not isinstance(loader_record, dict) or loader_record.get("sha256") != expected_loader_sha256:
        fail("musl loader manifest identity mismatch")

    overlay_dir = Path(__file__).resolve().parent / "initramfs"
    overlay_manifest = manifest.get("overlay", {})
    verified_overlay: dict[str, Entry] = {}
    for name, mode in OVERLAY_FILES.items():
        expected = read(overlay_dir / name)
        if name == "default.prop" and target != "radar_puffin":
            expected = expected.replace(b"=radar_puffin\n", b"=" + target.encode("ascii") + b"\n")
        if name == "ota-source.conf":
            source_text = expected.decode()
            source_text = re.sub(
                r"^channel=.*$", f"channel={expected_update_channel}",
                source_text, count=1, flags=re.MULTILINE,
            )
            source_text = re.sub(
                r"libreecho-radar-puffin-(?:dev|stable)\.ota\.tar",
                f"libreecho-radar-puffin-{expected_update_channel}.ota.tar",
                source_text,
            )
            expected = source_text.encode()
        target_name = OVERLAY_TARGETS.get(name, name)
        entry = require_member(entries, target_name, sha256(expected), mode)
        record = overlay_manifest.get(name, {})
        if record != {"sha256": sha256(expected), "size": len(expected), "mode": f"{mode:04o}"}:
            fail(f"overlay manifest mismatch for {name}")
        verified_overlay[name] = entry

    core_license_root = overlay_dir / "usr/local/share/licenses/libreecho-core"
    core_license_files = sorted(path for path in core_license_root.rglob("*") if path.is_file())
    if not core_license_files:
        fail("LibreEcho core license bundle is empty")
    for source in core_license_files:
        if source.is_symlink():
            fail(f"core license input is a symlink: {source}")
        name = source.relative_to(overlay_dir).as_posix()
        expected = read(source)
        require_member(entries, name, sha256(expected), 0o644)
        record = overlay_manifest.get(name, {})
        if record != {"sha256": sha256(expected), "size": len(expected), "mode": "0644"}:
            fail(f"core license overlay manifest mismatch for {name}")

    control = init
    if init.data != read(overlay_dir / "libreecho-init"):
        fail("runtime /init is not byte-identical to audited libreecho-init")
    init_record = overlay_manifest.get("init", {})
    if init_record != {
        "sha256": INIT_SHA256,
        "size": len(control.data),
        "mode": "0755",
        "source": "libreecho-init",
    }:
        fail("runtime /init overlay manifest mismatch")
    reconcile = verified_overlay["libreecho-reconcile-features"]
    for marker in (
        b"FEATURE_RECONCILE_ETC_ROOT:-/etc",
        b"$ETC_ROOT/libreecho/service-profile",
        b"$ETC_ROOT/libreecho/feature-policy",
        b"integrations & 1",
        b"integrations & 16",
        b'feature_runtime_mounted "$RUN_ROOT/libreecho/features/$feature/root"',
        b"FEATURE_RECONCILE_MOUNTINFO:-/proc/self/mountinfo",
        b"\"$script\" start",
        b"feature-services-reconcile-failed",
        b"shared_discovery_active",
        b"LIBREECHO_SHARED_DISCOVERY=1",
        b"feature-reconcile-shared-discovery-owner:airplayd",
    ):
        if marker not in reconcile.data:
            fail(f"feature reconciliation helper lacks {marker!r}")
    # V3 payloads exist only as authenticated generation mounts; a legacy v2
    # tree on /data must never satisfy the reconciler's payload gate.
    if b"libreecho/features/$feature/payload.squashfs" in reconcile.data:
        fail("feature reconciliation helper is gated on the legacy v2 payload tree")
    mdns_entry = verified_overlay["libreecho-mdnsd"]
    for marker in (
        b"MDNS_RUNTIME_ROOT:-/usr/local/lib/libreecho-mdns/root",
        b"mdns-shared-runtime-unavailable",
        b"mdns-competing-responder-refused",
        b"mdns-competing-responder:",
        b"mdns-orphan-reconcile:",
        b"$RUNTIME_ROOT/run",
        b"$RUNTIME_ROOT/var/lib/dbus",
        b"$BB chroot",
        b"ld-linux-armhf.so.3",
        b"avahi-daemon",
        b"dbus-daemon",
    ):
        if marker not in mdns_entry.data:
            fail(f"shared mDNS init entry lacks {marker!r}")
    for marker in (b"/data/libreecho/features/", b"payload.squashfs"):
        if marker in mdns_entry.data:
            fail("shared mDNS init entry is gated on a feature payload mount")
    if b"avahi-daemon" in control.data and b"system_bus_socket" not in control.data:
        fail("libreecho-init does not start the shared discovery runtime")
    validate_mdns(
        entries, manifest, expected_mdns_runtime_manifest_sha256,
        expected_service_profile,
    )
    for marker in (
        b"/run/libreecho-control/runme", b"functionfs", b"/dev/stpwmt", b"/dev/stpbt",
        b"for role_sx in /sys/class/usb_role/*/role; do",
        b'printf device > "$role_sx"', b"usb-role-pinned-device:",
        b"usb-role-pin-failed:",
    ):
        if marker not in control.data:
            fail(f"libreecho-init lacks {marker!r}")
    for forbidden in (
        b"FASTBOOT_PLEASE", b"PARTNAME=expdb", b"/sys/class/block/mmcblk0p7",
        b"printf FASTBOOT_PLEASE", b"expdb-sector",
    ):
        if forbidden in control.data:
            fail(f"libreecho-init contains forbidden expdb marker path {forbidden!r}")
    adbd_launches = tuple(
        line.strip() for line in control.data.splitlines()
        if line.lstrip().startswith(b"/sbin/adbd ")
    )
    if adbd_launches != (
        b"/sbin/adbd --device_banner=device </dev/null >/tmp/adbd.log 2>&1 &",
    ):
        fail(f"unexpected ARM32 adbd launch contract: {adbd_launches!r}")
    for forbidden in (b"/proc/hps/enabled", b"scaling_governor", b"cpuidle"):
        if forbidden in control.data:
            fail(f"libreecho-init contains forbidden policy override {forbidden!r}")
    properties = verified_overlay["default.prop"]
    for setting in (b"ro.boot.selinux=permissive", b"ro.secure=0", b"ro.debuggable=1", b"ro.adb.secure=0"):
        if setting not in properties.data.splitlines():
            fail(f"root-ADB property contract lacks {setting!r}")
    if any(name.startswith("res/") or name in {"sbin/recovery", "sbin/multi_init"} for name in entries):
        fail("unneeded stock recovery workload remains in initramfs")
    if expected_feature_policy == "exclude":
        if expected_service_profile != "diagnostic":
            fail("feature exclusion is not paired with the diagnostic service profile")
        for feature in ("airplay", "tts", "wakeword", "stt", "assistant"):
            record = manifest.get(feature, {"enabled": False})
            if not isinstance(record, dict) or record.get("enabled") is not False:
                fail(f"feature exclusion manifest enables {feature}")
        if b"FEATURE_POLICY" not in control.data or b"feature-services-excluded" not in control.data:
            fail("feature exclusion is not enforced by init")
    elif expected_feature_policy == "redistributable":
        if expected_service_profile != "production":
            fail("redistributable feature policy manifest mismatch")
        for feature in ("airplay", "tts", "stt", "assistant"):
            record = manifest.get(feature, {"enabled": False})
            if not isinstance(record, dict) or record.get("enabled") is not True:
                fail(f"redistributable feature policy manifest mismatch: {feature} disabled")
        wakeword = manifest.get("wakeword", {"enabled": False})
        if not isinstance(wakeword, dict) or wakeword.get("enabled") is not False:
            fail("redistributable feature policy manifest mismatch: wakeword enabled")
        if b"ui-services-redistributable-without-wakeword" not in control.data:
            fail("redistributable feature policy manifest mismatch: init graph marker missing")
    elif expected_feature_policy == "community-noncommercial":
        if expected_service_profile != "production":
            fail("community-noncommercial feature policy manifest mismatch")
        for feature in ("airplay", "tts", "wakeword", "stt", "assistant"):
            record = manifest.get(feature, {"enabled": False})
            if not isinstance(record, dict) or record.get("enabled") is not True:
                fail(
                    "community-noncommercial feature policy manifest mismatch: "
                    f"{feature} disabled"
                )
        if b"ui-services-community-noncommercial-with-wakeword" not in control.data:
            fail(
                "community-noncommercial feature policy manifest mismatch: "
                "init graph marker missing"
            )
    for name, entry in entries.items():
        info = elf_info(entry.data)
        if info is not None and info[:2] != (1, 40):
            fail(f"non-ARM32 ELF member {name}: {info[:2]}")
    validate_ssh(entries, manifest, expected_dropbear_sha256,
                 expected_dropbearkey_sha256, expected_scp_sha256)
    connectivity_ok = validate_connectivity(entries, manifest, schema_version)
    validate_recovery_ap(entries, manifest)
    return connectivity_ok


def load_recovery_ap_lock() -> dict[str, Any]:
    """Read and structurally validate the checked-in recovery-AP SOURCE.lock."""
    path = RECOVERY_AP_LOCK_PATH
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"ERROR: recovery-AP SOURCE.lock is unavailable: {path}")
    try:
        lock = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: recovery-AP SOURCE.lock is not valid JSON: {exc}") from exc
    if not isinstance(lock, dict):
        raise SystemExit("ERROR: recovery-AP SOURCE.lock is not a JSON object")
    components = lock.get("components")
    first_party = lock.get("first_party")
    if not isinstance(components, dict) or not isinstance(first_party, dict):
        raise SystemExit("ERROR: recovery-AP SOURCE.lock lacks component records")
    if set(components) | set(first_party) != set(RECOVERY_AP_COMPONENTS):
        raise SystemExit("ERROR: recovery-AP SOURCE.lock does not describe the pinned set")
    return lock


def validate_recovery_ap_source_lock(provenance: dict[str, Any],
                                     source_offer: object) -> None:
    """Bind the on-image metadata provenance to the checked-in SOURCE.lock.

    The metadata is a self-declared document staged by whichever build produced
    it, so it is only trustworthy when every component's name, version, https
    source URL, source SHA-256, licence and on-image licence copies match the
    repository's ``recovery-ap/SOURCE.lock``.  A metadata document written for an
    arbitrary binary is refused.
    """
    lock = load_recovery_ap_lock()
    components = lock["components"]
    first_party = lock["first_party"]
    on_image = lock.get("on_image")
    on_image_licenses = on_image.get("licenses") if isinstance(on_image, dict) else None
    if not isinstance(on_image_licenses, dict):
        on_image_licenses = {}

    for name, record in components.items():
        entry = provenance.get(name)
        if not isinstance(entry, dict):
            raise SystemExit(f"ERROR: recovery-AP metadata provenance is missing for {name}")
        for field in ("version", "license", "source_url", "source_sha256",
                      "source_license", "artifact"):
            if entry.get(field) != record.get(field):
                raise SystemExit(
                    f"ERROR: recovery-AP metadata {field} does not match SOURCE.lock: {name}"
                )
        if record.get("source_license_secondary") and (
                entry.get("source_license_secondary") != record["source_license_secondary"]):
            raise SystemExit(
                f"ERROR: recovery-AP metadata secondary licence does not match SOURCE.lock: {name}"
            )
        if entry.get("image_path") != RECOVERY_AP_COMPONENTS[name]:
            raise SystemExit(
                f"ERROR: recovery-AP metadata image path does not match the pinned target: {name}"
            )
        if list(entry.get("image_license", [])) != list(on_image_licenses.get(name, [])):
            raise SystemExit(
                f"ERROR: recovery-AP metadata on-image licences do not match SOURCE.lock: {name}"
            )

    for name, record in first_party.items():
        entry = provenance.get(name)
        if not isinstance(entry, dict):
            raise SystemExit(f"ERROR: recovery-AP metadata provenance is missing for {name}")
        for field in ("license", "source_url", "source_path", "artifact"):
            if entry.get(field) != record.get(field):
                raise SystemExit(
                    f"ERROR: recovery-AP metadata {field} does not match SOURCE.lock: {name}"
                )
        if entry.get("image_path") != RECOVERY_AP_COMPONENTS[name]:
            raise SystemExit(
                f"ERROR: recovery-AP metadata image path does not match the pinned target: {name}"
            )

    lock_offer = lock.get("source_offer")
    if not isinstance(lock_offer, dict) or not isinstance(source_offer, dict):
        raise SystemExit("ERROR: recovery-AP metadata has no source offer to bind to SOURCE.lock")
    if sorted(source_offer.get("components", [])) != sorted(lock_offer.get("components", [])):
        raise SystemExit("ERROR: recovery-AP source-offer components do not match SOURCE.lock")
    if (str(source_offer.get("statement", "")).strip() !=
            str(lock_offer.get("statement", "")).strip()):
        raise SystemExit("ERROR: recovery-AP source-offer statement does not match SOURCE.lock")
    if (str(source_offer.get("description", "")).strip() !=
            str(lock_offer.get("description", "")).strip()):
        raise SystemExit("ERROR: recovery-AP source-offer description does not match SOURCE.lock")


def validate_recovery_ap(entries: dict[str, Entry], manifest: dict[str, object]) -> None:
    """Verify the pinned recovery-AP bundle and its interface helpers.

    When the bundle is disabled no component binary may be present.  When it is
    enabled every compiled component (hostapd, dnsmasq, iw and the compiled
    evdev button detector) must be present as a static ARM32 ELF whose bytes
    match the hash the manifest recorded and the staged metadata file, and the
    overlay probes/interface helpers must ship at their pinned paths.  The
    button detector is pinned here, not accepted as "a shell file exists".
    """
    record = manifest.get("recovery_ap", {"enabled": False})
    if not isinstance(record, dict) or not isinstance(record.get("enabled"), bool):
        fail("recovery-AP manifest record is malformed")
    assert isinstance(record, dict)

    if not record["enabled"]:
        present = sorted(
            path for path in RECOVERY_AP_COMPONENTS.values() if path in entries
        )
        if present or RECOVERY_AP_METADATA in entries:
            fail(f"recovery-AP bundle is disabled but members are present: {present}")
        return

    for key, relative in RECOVERY_AP_OVERLAY.items():
        if record.get(key) != f"/{relative}":
            fail(f"recovery-AP manifest {key} path mismatch")
        if relative not in entries:
            fail(f"recovery-AP interface helper missing: {relative}")
        entry = entries[relative]
        if not stat.S_ISREG(entry.mode) or stat.S_IMODE(entry.mode) != 0o755:
            fail(f"recovery-AP interface helper mode mismatch: {relative}")
    if record.get("marker") != RECOVERY_AP_MARKER:
        fail("recovery-AP marker path mismatch")
    if record.get("button_detector") != (
        "/" + RECOVERY_AP_COMPONENTS["libreecho-recovery-button"]
    ):
        fail("recovery-AP button detector path mismatch")

    components = record.get("components")
    if not isinstance(components, dict) or set(components) != set(RECOVERY_AP_COMPONENTS):
        fail("recovery-AP component inventory does not match the pinned set")
    assert isinstance(components, dict)
    for name, relative in RECOVERY_AP_COMPONENTS.items():
        spec = components[name]
        if (not isinstance(spec, dict) or spec.get("path") != f"/{relative}" or
                spec.get("mode") != "0755"):
            fail(f"recovery-AP component path/mode mismatch: {name}")
        if relative not in entries:
            fail(f"recovery-AP component missing from initramfs: {relative}")
        entry = entries[relative]
        if not stat.S_ISREG(entry.mode) or stat.S_IMODE(entry.mode) != 0o755:
            fail(f"recovery-AP component mode mismatch: {name}")
        if sha256(entry.data) != spec.get("sha256") or len(entry.data) != spec.get("size"):
            fail(f"recovery-AP component identity mismatch: {name}")
        info = elf_info(entry.data)
        if info is None:
            fail(f"recovery-AP component is not an ELF: {name}")
        assert info is not None
        if info[0] != 1 or info[1] != 40:
            fail(f"recovery-AP component is not ARM32 ELF: {name}")
        if info[2] != 0x05000400 or info[3] is not None or info[4] != () or info[5]:
            fail(f"recovery-AP component is not a static EABI5 binary: {name}")

    metadata_entry = entries.get(RECOVERY_AP_METADATA)
    if metadata_entry is None:
        fail("recovery-AP staged metadata is missing")
    assert metadata_entry is not None
    metadata_record = record.get("metadata")
    if (not isinstance(metadata_record, dict) or
            metadata_record.get("path") != f"/{RECOVERY_AP_METADATA}" or
            metadata_record.get("sha256") != sha256(metadata_entry.data) or
            metadata_record.get("size") != len(metadata_entry.data) or
            metadata_record.get("mode") != "0644"):
        fail("recovery-AP metadata manifest record mismatch")
    metadata: object = None
    try:
        metadata = json.loads(metadata_entry.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        fail("recovery-AP metadata is not valid JSON")
    binaries = metadata.get("binaries") if isinstance(metadata, dict) else None
    if not isinstance(binaries, dict) or set(binaries) != set(RECOVERY_AP_COMPONENTS):
        fail("recovery-AP metadata binaries do not match the pinned set")
    assert isinstance(binaries, dict)
    for name in RECOVERY_AP_COMPONENTS:
        record_hash = binaries[name].get("sha256") if isinstance(binaries[name], dict) else None
        if record_hash != sha256(entries[RECOVERY_AP_COMPONENTS[name]].data):
            fail(f"recovery-AP metadata hash mismatch: {name}")

    # The staged metadata is the on-image attribution: it must carry the licence,
    # source, and corresponding-source offer for every redistributed binary, not
    # just their hashes.  A disabled bundle carries neither, so this runs only
    # for an enabled one.
    if not isinstance(metadata, dict) or metadata.get("schema") != RECOVERY_AP_METADATA_SCHEMA:
        fail("recovery-AP metadata schema is not the pinned contract")
    assert isinstance(metadata, dict)
    provenance = metadata.get("components")
    if not isinstance(provenance, dict) or set(provenance) != set(RECOVERY_AP_COMPONENTS):
        fail("recovery-AP metadata carries no provenance for the pinned set")
    assert isinstance(provenance, dict)
    offer = metadata.get("source_offer")
    if not isinstance(offer, dict) or not str(offer.get("statement", "")).strip():
        fail("recovery-AP metadata carries no corresponding-source statement")
    assert isinstance(offer, dict)
    offer_components = offer.get("components")
    offered = set(offer_components) if isinstance(offer_components, list) else set()
    for name, relative in RECOVERY_AP_COMPONENTS.items():
        record = provenance[name]
        if not isinstance(record, dict):
            fail(f"recovery-AP metadata provenance is malformed: {name}")
        assert isinstance(record, dict)
        licence = record.get("license")
        source_url = record.get("source_url")
        if not isinstance(licence, str) or not licence.strip():
            fail(f"recovery-AP metadata provenance declares no licence: {name}")
        assert isinstance(licence, str)
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            fail(f"recovery-AP metadata provenance has no https source URL: {name}")
        if record.get("image_path") != relative:
            fail(f"recovery-AP metadata provenance image path mismatch: {name}")
        source_path = record.get("source_path")
        if not (isinstance(source_path, str) and source_path):
            source_sha = record.get("source_sha256")
            if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
                fail(f"recovery-AP metadata provenance has no pinned source SHA-256: {name}")
            if not isinstance(record.get("source_license"), str) or not record["source_license"]:
                fail(f"recovery-AP metadata provenance names no licence file: {name}")
            image_license = record.get("image_license")
            if not isinstance(image_license, list) or not image_license:
                fail(f"recovery-AP metadata provenance names no on-image licence copy: {name}")
            assert isinstance(image_license, list)
            for licence_name in image_license:
                if not isinstance(licence_name, str) or not licence_name:
                    fail(f"recovery-AP metadata provenance has a malformed licence name: {name}")
                staged = f"{RECOVERY_AP_LICENSE_ROOT}/{licence_name}"
                if staged not in entries:
                    fail(f"recovery-AP on-image licence copy is missing: {staged}")
            if "GPL" in licence and name not in offered:
                fail(f"recovery-AP GPL component has no corresponding-source offer: {name}")

    # The attribution above is self-declared by the staged document; bind it to
    # the repository's checked-in pins so an image cannot claim a component the
    # SOURCE.lock does not list, or altered version/URL/source-hash/licence.
    validate_recovery_ap_source_lock(provenance, offer)


def system_map_physical_end(path: Path) -> int:
    symbols: dict[str, int] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3:
            try:
                symbols.setdefault(fields[2], int(fields[0], 16))
            except ValueError:
                pass
    if "_text" not in symbols or "_end" not in symbols:
        fail("System.map lacks _text or _end")
    return KERNEL_ADDR + symbols["_end"] - symbols["_text"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot-envelope", type=Path, required=True)
    parser.add_argument("--zimage", type=Path, required=True)
    parser.add_argument("--system-map", type=Path, required=True)
    parser.add_argument("--expected-system-map-sha256", default=SYSTEM_MAP_SHA256)
    parser.add_argument("--ramdisk", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--boot-image", type=Path, required=True)
    parser.add_argument("--expected-boot-sha256", required=True)
    parser.add_argument("--expected-zimage-sha256", default=ZIMAGE_SHA256)
    parser.add_argument("--expected-audio-probe-sha256",
                        help="require this static ARM32 audio probe in the initramfs")
    parser.add_argument("--expected-tinyplay-sha256",
                        help="require this static ARM32 TinyALSA playback utility")
    parser.add_argument("--expected-tinycap-sha256",
                        help="require this static ARM32 TinyALSA capture utility")
    parser.add_argument("--expected-tinymix-sha256",
                        help="require this static ARM32 TinyALSA mixer utility")

    parser.add_argument("--expected-iwconfig-sha256",
                        help="require this static ARM32 wireless-tools iwconfig utility")
    parser.add_argument("--expected-wpa-supplicant-sha256",
                        help="require this exact static ARM32 wpa_supplicant")
    parser.add_argument("--expected-mdns-runtime-manifest-sha256",
                        help="require this shared mDNS runtime manifest hash when "
                             "the boot-contained discovery runtime is staged")
    parser.add_argument("--expected-image-profile", choices=("development", "ota"), required=True)
    parser.add_argument("--expected-service-profile", choices=("diagnostic", "production"),
                        required=True)
    parser.add_argument("--expected-feature-policy",
        choices=("exclude", "preserve", "redistributable", "community-noncommercial"),
        required=True,
    )
    parser.add_argument("--expected-update-channel", choices=("dev", "stable"),
                        required=True)
    parser.add_argument("--expected-busybox-sha256", required=True)
    parser.add_argument("--expected-musl-loader-sha256", required=True)
    parser.add_argument("--expected-bootctl-sha256", required=True)
    parser.add_argument("--expected-update-verifier-sha256", required=True)
    parser.add_argument("--expected-ota-public-key-sha256", required=True)
    parser.add_argument("--expected-adbd-sha256", required=True)
    parser.add_argument("--expected-dropbear-sha256",
                        help="require this static ARM32 Dropbear server in the initramfs")
    parser.add_argument("--expected-dropbearkey-sha256",
                        help="require this static ARM32 Dropbear host-key utility in the initramfs")
    parser.add_argument("--expected-scp-sha256",
                        help="require the static ARM32 scp server-side executable in the initramfs")
    parser.add_argument("--expected-ui-manifest-sha256",
                        help="require this pinned LibreEcho-UI file manifest")
    parser.add_argument("--expected-ui-commit",
                        help="require this LibreEcho-UI source commit")
    parser.add_argument("--expected-ui-diff-sha256",
                        help="require this LibreEcho-UI source diff identity")
    parser.add_argument("--expected-nqptp-sha256",
                        help="require this static ARM32 NQPTP AirPlay 2 daemon")
    parser.add_argument("--expected-shairport-sync-sha256",
                        help="require this ARMHF Shairport Sync AirPlay 2 receiver")
    parser.add_argument("--expected-avahi-daemon-sha256",
                        help="require this ARMHF Avahi discovery daemon")
    parser.add_argument("--expected-dbus-daemon-sha256",
                        help="require this ARMHF D-Bus system daemon")
    parser.add_argument("--expected-airplay-payload-sha256",
                        help="require this external AirPlay 2 SquashFS payload")
    parser.add_argument("--expected-airplay-payload-size", type=int,
                        help="require this external AirPlay 2 payload size")
    parser.add_argument("--expected-tts-payload-sha256",
                        help="require this external two-voice TTS SquashFS payload")
    parser.add_argument("--expected-tts-payload-size", type=int,
                        help="require this external two-voice TTS payload size")
    parser.add_argument("--expected-wakeword-payload-sha256",
                        help="require this external openWakeWord SquashFS payload")
    parser.add_argument("--expected-wakeword-payload-size", type=int,
                        help="require this external openWakeWord payload size")
    parser.add_argument("--expected-stt-payload-sha256",
                        help="require this external English STT SquashFS payload")
    parser.add_argument("--expected-stt-payload-size", type=int,
                        help="require this external English STT payload size")
    parser.add_argument("--expected-boot-https-payload-sha256",
                        help="trusted boot transport input hash (defaults to assistant payload hash)")
    parser.add_argument("--expected-assistant-payload-sha256",
                        help="require this external streamed assistant SquashFS payload")
    parser.add_argument("--expected-assistant-payload-size", type=int,
                        help="require this external assistant payload size")
    parser.add_argument("--expected-dtb-sha256")
    parser.add_argument(
        "--expected-connectivity-bundle",
        choices=("none", CONNECTIVITY_BUNDLE_ID),
        default="none",
        help="require the initramfs to contain exactly this opt-in connectivity bundle",
    )
    add_target_arguments(parser)
    args = parser.parse_args()
    validate_target_arguments(parser, args)

    envelope, zimage, system_map, ramdisk, boot = map(
        read, (args.boot_envelope, args.zimage, args.system_map, args.ramdisk, args.boot_image)
    )
    manifest = json.loads(args.manifest.read_text())
    schema_version = manifest_schema(manifest)
    if len(envelope) != IMAGE_SIZE or envelope[:8] != ANDROID_MAGIC:
        fail("generated boot envelope is not an exact 16 MiB Android v0 envelope")
    if envelope != generate_boot_envelope() or sha256(envelope) != BOOT_ENVELOPE_SHA256:
        fail("boot envelope is not the canonical generated template")
    if sha256(zimage) != args.expected_zimage_sha256:
        fail("zImage hash mismatch")
    if sha256(system_map) != args.expected_system_map_sha256:
        fail("System.map hash mismatch")
    if manifest["inputs"].get("system_map", {}).get("sha256") != args.expected_system_map_sha256:
        fail("manifest System.map identity mismatch")
    if sha256(ramdisk) != manifest["initramfs"]["ramdisk_sha256"]:
        fail("ramdisk hash differs from manifest")
    if manifest["initramfs"].get("compression") != RAMDISK_COMPRESSION:
        fail("ramdisk compression differs from the kernel-supported xz contract")
    if sha256(boot) != args.expected_boot_sha256 or manifest["output"]["sha256"] != args.expected_boot_sha256:
        fail("boot-image hash mismatch")
    if manifest.get("status") != "PREPARED_NOT_FLASHED":
        fail("manifest deployment status changed")

    if len(boot) != IMAGE_SIZE or boot[:8] != ANDROID_MAGIC:
        fail("boot image is not the 16 MiB Android v0 envelope")
    envelope_fields = struct.unpack_from("<10I", envelope, 8)
    fields = struct.unpack_from("<10I", boot, 8)
    kernel_size, kernel_addr, ramdisk_size, ramdisk_addr = fields[:4]
    second_size, second_addr, tags_addr, page_size, dt_size, unused = fields[4:]
    if (kernel_addr, ramdisk_addr, second_size, second_addr, tags_addr, page_size, dt_size, unused) != (
        KERNEL_ADDR, RAMDISK_ADDR, 0, envelope_fields[5], TAGS_ADDR, PAGE, 0, envelope_fields[9]
    ):
        fail("Android header address/geometry contract mismatch")
    if not boot[64:576].startswith(b"bootopt=64S3,32N2,32N2"):
        fail("bootopt no longer selects the proven 32-bit path")
    envelope_header = bytearray(envelope[:PAGE])
    output_header = bytearray(boot[:PAGE])
    for start, end in ((8, 12), (16, 24), (576, 608)):
        envelope_header[start:end] = b"\0" * (end - start)
        output_header[start:end] = b"\0" * (end - start)
    if envelope_header != output_header:
        fail("Android header differs from generated envelope outside payload fields")

    kernel = boot[PAGE:PAGE + kernel_size]
    validate_mkimg_header(kernel)
    output_mkimg = bytearray(kernel[:MKIMG_SIZE])
    expected_mkimg = bytearray(MKIMG_SIZE)
    expected_mkimg[:4] = MKIMG_MAGIC
    expected_mkimg[8:14] = b"KERNEL"
    output_mkimg[4:8] = b"\0" * 4
    if expected_mkimg != output_mkimg:
        fail("MediaTek KERNEL header differs from generated constants")
    payload_size = struct.unpack_from("<I", kernel, 4)[0]
    if kernel_size != MKIMG_SIZE + payload_size:
        fail("Android kernel size disagrees with the MediaTek payload size")
    payload = kernel[MKIMG_SIZE:MKIMG_SIZE + payload_size]
    if payload[:len(zimage)] != zimage:
        fail("zImage is not byte-identical inside MediaTek payload")
    if struct.unpack_from("<I", zimage, 0x24)[0] != ZIMAGE_MAGIC:
        fail("zImage magic mismatch")
    if struct.unpack_from("<II", zimage, 0x28) != (0, len(zimage)):
        fail("zImage range fields mismatch")
    dtb = payload[len(zimage):]
    if len(dtb) != DTB_SIZE or dtb[:4] != FDT_MAGIC or struct.unpack_from(">I", dtb, 4)[0] != DTB_SIZE:
        fail("padded appended DTB contract mismatch")
    expected_dtb = args.expected_dtb_sha256
    if expected_dtb is None:
        fail("--expected-dtb-sha256 is required for a supplied DTB")
    raw_size = manifest["inputs"]["dtb_raw_size"]
    if not isinstance(raw_size, int) or not 8 <= raw_size <= DTB_SIZE:
        fail("manifest raw DTB size is invalid")
    raw = bytearray(dtb[:raw_size])
    struct.pack_into(">I", raw, 4, raw_size)
    if manifest["inputs"]["dtb_raw_sha256"] != expected_dtb or sha256(bytes(raw)) != expected_dtb:
        fail("raw EVT DTB identity mismatch")
    if any(dtb[raw_size:]):
        fail("EVT DTB padding is nonzero")


    ramdisk_offset = align(PAGE + kernel_size)
    if manifest["package"]["android"]["ramdisk_file_offset"] != f"0x{ramdisk_offset:x}":
        fail("manifest ramdisk file offset mismatch")
    if boot[ramdisk_offset:ramdisk_offset + ramdisk_size] != ramdisk:
        fail("ramdisk is not byte-identical inside boot image")
    kernel_padding = boot[PAGE + kernel_size:ramdisk_offset]
    ramdisk_end_file = ramdisk_offset + ramdisk_size
    trailing = boot[align(ramdisk_end_file):]
    if any(kernel_padding) or any(boot[ramdisk_end_file:align(ramdisk_end_file)]) or any(trailing):
        fail("section or trailing padding is nonzero")
    if boot[576:608] != android_id(kernel, ramdisk, b"", b""):
        fail("Android v0 ID mismatch")

    loaded_end = KERNEL_ADDR + payload_size
    runtime_end = system_map_physical_end(args.system_map)
    ramdisk_end = RAMDISK_ADDR + ramdisk_size
    if not (
        loaded_end < runtime_end <= ATF_START < ATF_END <= RAMDISK_ADDR <
        ramdisk_end <= RAMDISK_END_LIMIT < TAGS_ADDR
    ):
        fail("physical boot envelope overlaps or is out of order")

    connectivity_enabled = validate_initramfs(
        ramdisk, manifest, schema_version, args.expected_image_profile,
        args.expected_service_profile, args.expected_feature_policy,
        args.expected_update_channel, args.expected_busybox_sha256, args.expected_musl_loader_sha256,
        args.expected_bootctl_sha256, args.expected_update_verifier_sha256,
        args.expected_ota_public_key_sha256, args.expected_adbd_sha256,
        args.expected_audio_probe_sha256,
        args.expected_tinyplay_sha256, args.expected_tinycap_sha256,
        args.expected_tinymix_sha256,
        args.expected_iwconfig_sha256,
        args.expected_dropbear_sha256, args.expected_dropbearkey_sha256,
        args.expected_scp_sha256,
        args.expected_ui_manifest_sha256, args.expected_ui_commit,
        args.expected_ui_diff_sha256,
        args.expected_airplay_payload_sha256, args.expected_airplay_payload_size,
        args.expected_tts_payload_sha256, args.expected_tts_payload_size,
        args.expected_wakeword_payload_sha256,
        args.expected_wakeword_payload_size,
        args.expected_stt_payload_sha256, args.expected_stt_payload_size,
        args.expected_assistant_payload_sha256,
        args.expected_assistant_payload_size,
        args.expected_nqptp_sha256, args.expected_shairport_sync_sha256,
        args.expected_avahi_daemon_sha256, args.expected_dbus_daemon_sha256,
        args.expected_wpa_supplicant_sha256,
        args.expected_mdns_runtime_manifest_sha256,
        expected_target=args.target,
        target_descriptor_sha256=args.target_descriptor_sha256,
        expected_boot_https_payload_sha256=args.expected_boot_https_payload_sha256,
    )
    expected_connectivity = args.expected_connectivity_bundle != "none"
    if connectivity_enabled != expected_connectivity:
        actual = CONNECTIVITY_BUNDLE_ID if connectivity_enabled else "none"
        fail(
            "connectivity bundle expectation mismatch: "
            f"expected={args.expected_connectivity_bundle} actual={actual}"
        )
    network_record = manifest.get("network", {})
    network_activation = (
        network_record.get("activation", "passive")
        if isinstance(network_record, dict) else "passive"
    )
    mdns_record = manifest.get("mdns", {})
    mdns_enabled = bool(
        isinstance(mdns_record, dict) and mdns_record.get("enabled") is True
    )
    print(
        "arm32_recovery_image_contract=PASS android_v0=yes mtk_wrapper=yes "
        "zimage=yes evt_dtb=yes initramfs_arm32=yes "
        f"fastboot_marker={'automatic' if args.expected_image_profile == 'development' else 'explicit-only'} "
        f"image_profile={args.expected_image_profile} service_profile={args.expected_service_profile} "
        f"feature_policy={args.expected_feature_policy} ota=yes "
        "root_adb_staged=yes runme=yes memory_disjoint=yes "
        f"connectivity_bundle={'yes' if connectivity_enabled else 'no'} "
        f"audio_tools={'yes' if args.expected_tinyplay_sha256 and args.expected_tinycap_sha256 and args.expected_tinymix_sha256 else 'no'} "
        f"airplay={'yes' if args.expected_airplay_payload_sha256 or (args.expected_nqptp_sha256 and args.expected_shairport_sync_sha256 and args.expected_avahi_daemon_sha256 and args.expected_dbus_daemon_sha256) else 'no'} "
        f"tts={'yes' if args.expected_tts_payload_sha256 else 'no'} "
        f"wakeword={'yes' if args.expected_wakeword_payload_sha256 else 'no'} "
        f"stt={'yes' if args.expected_stt_payload_sha256 else 'no'} "
        f"assistant={'yes' if args.expected_assistant_payload_sha256 else 'no'} "
        f"network_activation={network_activation} "
        f"mdns={'yes' if mdns_enabled else 'no'} status=PREPARED_NOT_FLASHED"
    )


if __name__ == "__main__":
    main()
