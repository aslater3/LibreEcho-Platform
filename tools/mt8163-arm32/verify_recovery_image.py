#!/usr/bin/env python3
"""Independent verifier for the MT8163 ARM32 recovery boot image."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast


ANDROID_MAGIC = b"ANDROID!"
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
SOURCE_SHA256 = "c0f52a3b079d214495cd3dd22f92fd85695d1b868c58b491a2edb933bc4f6d1a"
ZIMAGE_SHA256 = "4e144959eb0ffaee91b37d05a0f871863a74f4abb1bad0474c2fec358d5176a6"
SYSTEM_MAP_SHA256 = "527292112edd28e8facf2998eefe2224b08a05b193efc73634cd998e9113ba95"
CONNECTIVITY_BUNDLE_ID = "mt8163-v181-stock-v1"
CONNECTIVITY_STOCK_SYSTEM_SHA256 = "56540b3a9ac4437901a5510d9fb5e09b1a8d0cc229548f0b08bb5c22d78684fe"
CONNECTIVITY_EVIDENCE_MANIFEST_SHA256 = "d1eedd04efe0dbc78853f2b0f9357c092b4ca66242648908c0369956538441eb"
STOCK_DTB_SHA256 = "f44630ba28f503dd7503bc7cffa2ee96a319acf2f58f1456bb6f5ff23d57dee1"
PADDED_STOCK_DTB_SHA256 = "08b16ec39554d644d8cbdf8f5816559f85414ab45bc1901de46a7cd43dc286ed"
BUSYBOX_SHA256 = "d4c8fd2aea01abd851c703f39b29c0de748b2751e4e1a85cae570fa53ad8f4fb"
LOADER_SHA256 = "1063871174f1bd4f08f4d330e20b07aeb0820327ee739a4d8d1b644df842cb6b"
INIT_SHA256 = "33e1326be258cc7466b9feaad0ce0a2772b866780297c2b797ef78477b5ab834"
ADBD_SHA256 = "1c0d14afb1ce19494ee1da935e1076f49ff57e359d348262a28bb3d56abeb930"
OVERLAY_FILES = {
    "default.prop": 0o644,
    "profile": 0o644,
    "init.rc": 0o644,
    "init.recovery.mt8163.rc": 0o644,
    "libreecho-init": 0o755,
    "libreecho-update": 0o755,
    "libreecho-update-fetch": 0o755,
    "ota-source.conf": 0o644,
}
OVERLAY_TARGETS = {
    "profile": "etc/profile",
    "libreecho-update": "usr/local/sbin/libreecho-update",
    "libreecho-update-fetch": "usr/local/sbin/libreecho-update-fetch",
    "ota-source.conf": "etc/libreecho/ota-source.conf",
}
SSH_PASSWORD_HASH_RE = re.compile(
    rb"\$(?:1|5|6|2[abxy]?|y|gy)\$[^$:\r\n]{1,64}\$[^:\r\n]{1,512}\Z"
)
SSH_MEMBER_NAMES = {
    "sbin/dropbear", "sbin/dropbearkey", "etc/passwd", "etc/group",
    "etc/shells", "etc/shadow", "root", "etc/dropbear",
}
UI_BINARY_NAMES = {
    "usr/local/sbin/libreecho-web",
    "usr/local/sbin/libreecho-logd",
    "usr/local/sbin/libreecho-networkd",
    "usr/local/sbin/libreecho-timed",
    "usr/local/sbin/libreecho-audiod",
    "usr/local/sbin/libreecho-micd",
    "usr/local/sbin/libreecho-ledd",
    "usr/local/sbin/libreecho-btd",
    "usr/local/sbin/libreecho-airplayd",
    "usr/local/sbin/libreecho-wyomingd",
}
UI_INIT_NAMES = {
    "etc/init.d/libreecho-web.init",
    "etc/init.d/libreecho-logd.init",
    "etc/init.d/libreecho-networkd.init",
    "etc/init.d/libreecho-timed.init",
    "etc/init.d/libreecho-audiod.init",
    "etc/init.d/libreecho-micd.init",
    "etc/init.d/libreecho-ledd.init",
    "etc/init.d/libreecho-btd.init",
    "etc/init.d/libreecho-airplayd.init",
    "etc/init.d/libreecho-ttsd.init",
    "etc/init.d/libreecho-waked.init",
    "etc/init.d/libreecho-sttd.init",
    "etc/init.d/libreecho-agentd.init",
    "etc/init.d/libreecho-wyomingd.init",
}
UI_FIXED_NAMES = UI_BINARY_NAMES | UI_INIT_NAMES | {
    "etc/libreecho/web-config.json",
    "etc/libreecho/airplay2.conf",
    "etc/libreecho/ntp.conf",
    "usr/local/share/libreecho/ui-manifest.txt",
}
UI_OPTIONAL_NAMES = {"etc/libreecho/users"}
AIRPLAY_BINARY_NAMES = {
    "usr/local/sbin/nqptp", "usr/local/sbin/shairport-sync",
    "usr/local/sbin/avahi-daemon", "usr/local/sbin/dbus-daemon",
    "usr/local/sbin/libreecho-airplay-audio",
    "usr/local/sbin/libreecho-audio-engine",
}

CONNECTIVITY_FILES = {
    "system/bin/linker": (
        0o755, 630460, "73dc93e06a9ce0a76b5353f2c282f1ac3dd0dccd0e8e7f06fc20e5433ef4a3dc", (),
    ),
    "system/vendor/bin/wmt_loader": (
        0o755, 17992, "de9ee285a09a7db5b079233f7c9129c5484ecb6701b54da45e2a29f310e74ff9",
        ("libcutils.so", "libc++.so", "libdl.so", "libc.so", "libm.so"),
    ),
    "system/vendor/bin/wmt_launcher": (
        0o755, 31448, "1f34425d727ea64524c9edaeac5e6b295df7a6054703dcc79b164021560252e5",
        ("libcutils.so", "libc++.so", "libdl.so", "libc.so", "libm.so"),
    ),
    "lib/firmware/ROMv2_lm_patch_1_0_hdr.bin": (
        0o644, 128720, "b4460117f51a43f3284594ec08d8c8861ecc0e42b17820987da03ecabdebac1e", None,
    ),
    "lib/firmware/ROMv2_lm_patch_1_1_hdr.bin": (
        0o644, 50148, "10c4ed22a10b8a136bffd7ffce4d552300d76f8e593627d2a9841c3b11a5697e", None,
    ),
    "lib/firmware/WIFI_RAM_CODE_8163": (
        0o644, 373840, "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c", None,
    ),
    "lib/firmware/WMT_SOC.cfg": (
        0o644, 119, "302bd4462de99c028c04092e561c1500d65582ce42a93c4c72ccae6e2c99013d", None,
    ),
    "system/lib/libcutils.so": (
        0o644, 104436, "dcf249ceed2c84ab45454ff8fd3fa0624248b410962c4ea9e9e799610192542b",
        ("liblog.so", "libc++.so", "libdl.so", "libc.so", "libm.so"),
    ),
    "system/lib/libc++.so": (
        0o644, 575068, "38f15c7897307e65c9b9a13174782e7b79146e453b8b80e09128aae8b6ab1df5",
        ("libdl.so", "libc.so", "libm.so"),
    ),
    "system/lib/libdl.so": (
        0o644, 13640, "efb8d634212b215b53f8c95f2b8372e9139ee13dc74717b7d25999de97d5b1cc", (),
    ),
    "system/lib/libc.so": (
        0o644, 780476, "1254edac10625b1e7e123c20ea8d8f3175ad07014c9ddcca7bb3ea74db555357",
        ("libdl.so",),
    ),
    "system/lib/libm.so": (
        0o644, 132820, "3703abfae55405f1ca876cfaf5c8e41b0dafdd30d4ecec88cbd1100c5b0341ed",
        ("libc.so",),
    ),
    "system/lib/liblog.so": (
        0o644, 67460, "84e34e101618dae346cefca70c8cd866b92e6bcdec64246a130dcd12560410c0",
        ("libc.so", "libm.so"),
    ),
}

CONNECTIVITY_REFERENCE_FILES = {
    "init.connectivity.rc": {
        "sha256": "142c3f2239255dff573196daaf7da00687be9c5c54174dcbecfa309074d9d379",
        "size": 3167,
    },
    "ueventd.mt8163.rc": {
        "sha256": "b1d212a42d213b4b1412648e7501baf55aa3ee653236cdf10f650050e0ea325c",
        "size": 4255,
    },
}

CONNECTIVITY_SYMLINKS = {
    "vendor": "system/vendor",
    "system/vendor/firmware": "../../lib/firmware",
    "system/etc/firmware": "../../lib/firmware",
    "etc/firmware": "../lib/firmware",
    "lib/firmware/WIFI_RAM_CODE": "WIFI_RAM_CODE_8163",
}

CONNECTIVITY_HELPERS = {
    "sbin/wmt_configure": (
        428704, "2fa1c78546b3a0d35442ffa196f3eaa13b1ce4609b537332b016bc88ea663be2",
    ),
    "sbin/wmt_responder": (
        428796, "e20bdaf559165077ff8211c64ed38a10ecee1006641e94302cf14d3be397c350",
    ),
    "sbin/wmt_bt_on": (
        424540, "4365c1b1046bf2ce1045a3fbd4578ee21d8f1a9900a01cb0cde9cea478821d82",
    ),
    "sbin/wmt_stock_compat": (
        341184, "5be9b801153c79f85260b193c57a5ba5c4155f9fccbad47a794e9445e94d654c",
    ),
    "sbin/wmt_launcher": (
        428912, "6e65e46536bfea0b44f0887998a4d556338250d42609e13fbe6d7833a08187c3",
    ),
}

CONNECTIVITY_PATCH_ROUTES = {
    "lib/firmware/ROMv2_lm_patch_1_0_hdr.bin": (
        bytes((0x8A, 0x00)), bytes((0x22, 0x00, 0x06, 0x00)), 2,
        bytes((0x00, 0x00, 0x06, 0x00)),
    ),
    "lib/firmware/ROMv2_lm_patch_1_1_hdr.bin": (
        bytes((0x8A, 0x00)), bytes((0x21, 0x00, 0x0E, 0xF0)), 1,
        bytes((0x00, 0x00, 0x0E, 0xF0)),
    ),
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
                 expected_dropbearkey_sha256: str | None) -> bool:
    raw_ssh = manifest.get("ssh")
    if raw_ssh is None:
        ssh: dict[str, object] = {"enabled": False}
    elif not isinstance(raw_ssh, dict) or not isinstance(raw_ssh.get("enabled"), bool):
        fail("SSH manifest record is malformed")
    else:
        ssh = cast(dict[str, object], raw_ssh)

    expected_enabled = (
        expected_dropbear_sha256 is not None or
        expected_dropbearkey_sha256 is not None
    )
    if bool(ssh.get("enabled")) != expected_enabled:
        fail(
            "SSH bundle expectation mismatch: "
            f"expected={'enabled' if expected_enabled else 'disabled'} "
            f"actual={'enabled' if ssh.get('enabled') else 'disabled'}"
        )

    forbidden_ssh_names = sorted(
        name for name in entries
        if name.endswith("/authorized_keys") or name == "authorized_keys"
        or "/.ssh/" in name or name.endswith(("/id_rsa", "/id_ecdsa", "/id_ed25519"))
    )
    if forbidden_ssh_names:
        fail(f"SSH image contains forbidden key material: {forbidden_ssh_names}")

    if not expected_enabled:
        unexpected = sorted(name for name in SSH_MEMBER_NAMES if name in entries)
        if unexpected:
            fail(f"SSH bundle is disabled but members are present: {unexpected}")
        return False

    if expected_dropbear_sha256 is None or expected_dropbearkey_sha256 is None:
        fail("SSH binary identities are incomplete")
    expected_policy = {
        "enabled": True,
        "activation": "manual-only",
        "autostart": False,
        "authentication": "password-only",
        "public_key_auth": False,
        "root_login": True,
        "host_keys": "generated-ephemerally-under-/tmp/dropbear",
    }
    for key, value in expected_policy.items():
        if ssh.get(key) != value:
            fail(f"SSH policy changed for {key}: {ssh.get(key)!r}")
    raw_files = ssh.get("files")
    if not isinstance(raw_files, dict):
        fail("SSH file manifest is missing")
    files = cast(dict[str, object], raw_files)
    if set(files) != SSH_MEMBER_NAMES - {"root", "etc/dropbear"}:
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

    expected_accounts = {
        "etc/passwd": b"root:x:0:0:root:/root:/bin/sh\n",
        "etc/group": b"root:x:0:\n",
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

    shadow = entries.get("etc/shadow")
    if shadow is None or not stat.S_ISREG(shadow.mode) or stat.S_IMODE(shadow.mode) != 0o600:
        fail("SSH /etc/shadow is missing or has unsafe permissions")
    shadow_fields = shadow.data.rstrip(b"\n").split(b":")
    if len(shadow_fields) != 9 or shadow_fields[0] != b"root":
        fail("SSH /etc/shadow root record is malformed")
    if not SSH_PASSWORD_HASH_RE.fullmatch(shadow_fields[1]):
        fail("SSH /etc/shadow does not contain a supported salted root hash")
    if shadow.data.count(b"\n") != 1 or shadow.data.endswith(b"\n\n"):
        fail("SSH /etc/shadow must contain exactly one normalized record")
    if files.get("etc/shadow") != {
        "path": "/etc/shadow",
        "size": len(shadow.data),
        "mode": "0600",
        "secret_content_not_recorded": True,
    }:
        fail("SSH shadow manifest record is unsafe or changed")

    for name, mode in (("root", 0o755), ("etc/dropbear", 0o700)):
        entry = entries.get(name)
        if entry is None or not stat.S_ISDIR(entry.mode) or stat.S_IMODE(entry.mode) != mode:
            fail(f"SSH runtime directory contract changed: {name}")
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
    }:
        fail("iwconfig manifest record changed")
    return True


def validate_ui(entries: dict[str, Entry], manifest: dict[str, object],
                expected_manifest_sha256: str | None,
                expected_commit: str | None,
                expected_diff_sha256: str | None) -> bool:
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
        if name in UI_BINARY_NAMES:
            if elf_info(member.data) != (1, 40, 0x05000400, None, (), False):
                fail(f"UI binary is not static ARM32 hard-float: {name}")

    return True


def validate_connectivity(entries: dict[str, Entry], manifest: dict[str, object],
                          schema_version: int) -> bool:
    record = manifest.get("connectivity", {"enabled": False})
    if not isinstance(record, dict) or not isinstance(record.get("enabled"), bool):
        fail("connectivity manifest record is malformed")
    bundle_names = set(CONNECTIVITY_FILES) | set(CONNECTIVITY_HELPERS) | set(CONNECTIVITY_SYMLINKS)
    if not record["enabled"]:
        unexpected = sorted(name for name in bundle_names if name in entries)
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
    if record.get("id") != CONNECTIVITY_BUNDLE_ID:
        fail("connectivity bundle identity changed")
    if record.get("activation") != "manual-gates-only":
        fail("connectivity activation policy changed")
    if record.get("autostart") is not False:
        fail("connectivity autostart must remain disabled")
    expected_payload_bytes = sum(
        expected_size for _mode, expected_size, _expected_hash, _needed
        in CONNECTIVITY_FILES.values()
    ) + sum(expected_size for expected_size, _expected_hash in CONNECTIVITY_HELPERS.values())
    if record.get("stock_file_count") != len(CONNECTIVITY_FILES):
        fail("connectivity stock-file count changed")
    if record.get("helper_count") != len(CONNECTIVITY_HELPERS):
        fail("connectivity helper count changed")
    if record.get("payload_bytes") != expected_payload_bytes:
        fail("connectivity payload byte count changed")
    if record.get("provenance") != {
        "stock_system_a_sha256": CONNECTIVITY_STOCK_SYSTEM_SHA256,
        "evidence_manifest_sha256": CONNECTIVITY_EVIDENCE_MANIFEST_SHA256,
    }:
        fail("connectivity provenance changed")
    stock_root = record.get("stock_root")
    if not isinstance(stock_root, str) or not Path(stock_root).is_absolute():
        fail("connectivity stock-root provenance is not absolute")
    if record.get("reference_files_not_copied") != CONNECTIVITY_REFERENCE_FILES:
        fail("connectivity reference-file manifest mismatch")
    file_records = record.get("files")
    if not isinstance(file_records, dict) or set(file_records) != set(CONNECTIVITY_FILES):
        fail("connectivity stock-file manifest is incomplete")
    library_providers = {
        PurePosixPath(name).name: name
        for name in CONNECTIVITY_FILES if name.startswith("system/lib/")
    }
    for name, (mode, expected_size, expected_hash, needed) in CONNECTIVITY_FILES.items():
        entry = require_member(entries, name, expected_hash, mode)
        if len(entry.data) != expected_size:
            fail(f"connectivity member size mismatch for {name}")
        source = (
            "system/vendor/firmware/" + PurePosixPath(name).name
            if name.startswith("lib/firmware/") else name
        )
        expected_record: dict[str, object] = {
            "source": source,
            "sha256": expected_hash,
            "size": expected_size,
            "mode": f"{mode:04o}",
        }
        info = elf_info(entry.data)
        if needed is None:
            if info is not None:
                fail(f"connectivity firmware unexpectedly contains ELF: {name}")
        else:
            expected_info = (1, 40, 0x05000200, "/system/bin/linker", needed, True)
            if info != expected_info:
                fail(f"stock connectivity ELF contract mismatch for {name}: {info}")
            expected_record["elf"] = {
                "class": 1,
                "machine": 40,
                "flags": "0x05000200",
                "interpreter": "/system/bin/linker",
                "needed": list(needed),
                "dynamic": True,
            }
            for dependency in needed:
                if dependency not in library_providers:
                    fail(f"no staged provider for {name} dependency {dependency}")
        if file_records.get(name) != expected_record:
            fail(f"connectivity manifest record mismatch for {name}")

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

    if record.get("symlinks") != CONNECTIVITY_SYMLINKS:
        fail("connectivity symlink manifest mismatch")
    for name, target in CONNECTIVITY_SYMLINKS.items():
        entry = entries.get(name)
        if entry is None or not stat.S_ISLNK(entry.mode) or entry.data != target.encode():
            fail(f"connectivity symlink contract mismatch for {name}")
        resolved = resolve_relative_symlink(name, target)
        if resolved not in entries:
            fail(f"connectivity symlink dangles: {name} -> {target}")

    expected_patch_routing: dict[str, object] = {}
    for name, (expected_header, expected_route, expected_seq,
               expected_address) in CONNECTIVITY_PATCH_ROUTES.items():
        data = entries[name].data
        route = data[24:28]
        patch_count = route[0] >> 4
        download_seq = route[0] & 0x0F
        address = b"\0" + route[1:]
        if (data[22:24] != expected_header or route != expected_route or
                patch_count != len(CONNECTIVITY_PATCH_ROUTES) or
                download_seq != expected_seq or address != expected_address):
            fail(f"stock patch metadata changed for {name}")
        expected_patch_routing[name] = {
            "header": expected_header.hex(),
            "route": expected_route.hex(),
            "patch_count": patch_count,
            "download_seq": download_seq,
            "address": address.hex(),
        }
    if not strictly_equal(record.get("patch_routing"), expected_patch_routing):
        fail("connectivity patch-routing manifest mismatch")

    return True


def validate_initramfs(ramdisk: bytes, manifest: dict[str, object],
                       schema_version: int,
                       expected_image_profile: str,
                       expected_bootctl_sha256: str,
                       expected_update_verifier_sha256: str,
                       expected_ota_public_key_sha256: str,
                       expected_audio_probe_sha256: str | None,
                       expected_tinyplay_sha256: str | None,
                       expected_tinycap_sha256: str | None,
                       expected_tinymix_sha256: str | None,
                       expected_startup_audio_sha256: str | None,
                       expected_iwconfig_sha256: str | None,
                       expected_dropbear_sha256: str | None,
                       expected_dropbearkey_sha256: str | None,
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
                       expected_dbus_daemon_sha256: str | None) -> bool:
    if ramdisk[:4] != b"\x1f\x8b\x08\x00":
        fail("ramdisk gzip header is not deterministic")
    try:
        cpio = gzip.decompress(ramdisk)
    except gzip.BadGzipFile as exc:
        fail(f"ramdisk gzip is invalid: {exc}")
    entries = parse_newc(cpio)
    validate_archive_tree(entries)
    validate_symlinks(entries)
    validate_no_connectivity_autostart(entries)
    if manifest.get("image_profile") != expected_image_profile:
        fail("image profile manifest mismatch")
    require_member(
        entries, "etc/libreecho/image-profile",
        sha256((expected_image_profile + "\n").encode()), 0o644,
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
        if network.get("activation") != "automatic-after-adb-if-profile-present":
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
        wpa = require_member(entries, "sbin/wpa_supplicant", wpa_hash, 0o755)
        if elf_info(wpa.data) != (1, 40, 0x05000400, None, (), False):
            fail("wpa_supplicant is not static ARM32 hard-float")
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
                tts.get("voices") != ["southern-female", "alan"] or
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
            "usr/local/share/libreecho/tts/models/alan/model.onnx",
            "usr/local/share/libreecho/tts/models/alan/tokens.txt",
            "usr/local/share/libreecho/tts/models/southern-female/model.onnx",
            "usr/local/share/libreecho/tts/models/southern-female/tokens.txt",
        ):
            if required not in files:
                fail(f"external TTS payload member missing: {required}")
        for voice in ("alan", "southern-female"):
            prefix = f"usr/local/share/libreecho/tts/models/{voice}/espeak-ng-data/"
            if not any(str(relative).startswith(prefix) for relative in files):
                fail(f"external TTS payload lacks eSpeak data for {voice}")
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
        ):
            if required not in files:
                fail(f"external AirPlay payload member missing: {required}")
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
        "etc/audio/windows95-startup.wav",
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
    elif ((audio.get("enabled") and expected_startup_audio_sha256 is None) or
          sorted(name for name in audio_names
                 if name in entries and name != "etc/audio/windows95-startup.wav")):
        fail("audio assets are enabled without expected identities")
    if expected_startup_audio_sha256 is not None:
        if not audio.get("enabled"):
            fail("startup audio is expected but audio manifest is disabled")
        raw_startup = audio.get("startup_playback")
        if not isinstance(raw_startup, dict):
            fail("startup audio manifest record is incomplete")
        startup_record = cast(dict[str, object], raw_startup)
        if startup_record.get("sha256") != expected_startup_audio_sha256:
            fail("startup audio manifest hash mismatch")
        startup_path = startup_record.get("path")
        if not isinstance(startup_path, str) or not Path(startup_path).is_absolute():
            fail("startup audio manifest path is not absolute")
        startup = require_member(
            entries, "etc/audio/windows95-startup.wav",
            expected_startup_audio_sha256, 0o644,
        )
        expected_startup_record = {
            "path": startup_path,
            "sha256": expected_startup_audio_sha256,
            "size": len(startup.data),
            "mode": "0644",
            "format": {
                "channels": 2,
                "sample_rate": 48000,
                "sample_width_bits": 16,
                "compression": "NONE",
            },
            "route": "hpr-only",
            "pcm_volume": "103/103",
            "pcm_db": "-12.0",
            "hp_driver_gain": "6/6",
            "lineout_dac_switches": "off",
            "playback_device": "0:23",
            "plays_once": True,
        }
        if startup_record != expected_startup_record:
            fail("startup audio manifest record mismatch")
        if audio.get("activation") != "automatic-after-successful-init":
            fail("startup audio activation policy changed")
    elif "etc/audio/windows95-startup.wav" in entries:
        fail("startup audio is present without an expected identity")
    if sha256(cpio) != manifest["initramfs"]["cpio_sha256"]:
        fail("manifest cpio hash mismatch")
    if any(entry.uid or entry.gid or entry.mtime for entry in entries.values()):
        fail("initramfs ownership or mtime is not normalized")

    init = require_member(entries, "init", INIT_SHA256, 0o755)
    adbd = require_member(entries, "sbin/adbd", ADBD_SHA256, 0o750)
    busybox = require_member(entries, "bin/busybox", BUSYBOX_SHA256, 0o755)
    loader = require_member(entries, "lib/ld-musl-armhf.so.1", LOADER_SHA256, 0o755)
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
    for name, target in symlinks.items():
        entry = entries.get(name)
        if entry is None or not stat.S_ISLNK(entry.mode) or entry.data != target:
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

    overlay_dir = Path(__file__).resolve().parent / "initramfs"
    overlay_manifest = manifest.get("overlay", {})
    verified_overlay: dict[str, Entry] = {}
    for name, mode in OVERLAY_FILES.items():
        expected = read(overlay_dir / name)
        target_name = OVERLAY_TARGETS.get(name, name)
        entry = require_member(entries, target_name, sha256(expected), mode)
        record = overlay_manifest.get(name, {})
        if record != {"sha256": sha256(expected), "size": len(expected), "mode": f"{mode:04o}"}:
            fail(f"overlay manifest mismatch for {name}")
        verified_overlay[name] = entry

    control = verified_overlay["libreecho-init"]
    if init.data != control.data:
        fail("runtime /init is not byte-identical to audited libreecho-init")
    init_record = overlay_manifest.get("init", {})
    if init_record != {
        "sha256": INIT_SHA256,
        "size": len(control.data),
        "mode": "0755",
        "source": "libreecho-init",
    }:
        fail("runtime /init overlay manifest mismatch")
    for marker in (
        b"FASTBOOT_PLEASE", b"/tmp/runme", b"functionfs", b"/dev/stpwmt", b"/dev/stpbt",
        b"PARTNAME=expdb", b"/sys/class/block/mmcblk0p7", b"20480", b"bs=15 count=1",
        b"stat -c '%t:%T'",
    ):
        if marker not in control.data:
            fail(f"libreecho-init lacks {marker!r}")
    adbd_launches = tuple(
        line.strip() for line in control.data.splitlines()
        if line.lstrip().startswith(b"/sbin/adbd ")
    )
    if adbd_launches != (
        b"/sbin/adbd --root_seclabel=u:r:su:s0 --device_banner=device </dev/null >/tmp/adbd.log 2>&1 &",
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
    for name, entry in entries.items():
        info = elf_info(entry.data)
        if info is not None and info[:2] != (1, 40):
            fail(f"non-ARM32 ELF member {name}: {info[:2]}")
    validate_ssh(entries, manifest, expected_dropbear_sha256, expected_dropbearkey_sha256)
    return validate_connectivity(entries, manifest, schema_version)


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
    parser.add_argument("--source-boot", type=Path, required=True)
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
    parser.add_argument("--expected-startup-audio-sha256",
                        help="require this pinned stereo 48kHz PCM16 startup WAV")
    parser.add_argument("--expected-iwconfig-sha256",
                        help="require this static ARM32 wireless-tools iwconfig utility")
    parser.add_argument("--expected-image-profile", choices=("development", "ota"), required=True)
    parser.add_argument("--expected-bootctl-sha256", required=True)
    parser.add_argument("--expected-update-verifier-sha256", required=True)
    parser.add_argument("--expected-ota-public-key-sha256", required=True)
    parser.add_argument("--expected-dropbear-sha256",
                        help="require this static ARM32 Dropbear server in the initramfs")
    parser.add_argument("--expected-dropbearkey-sha256",
                        help="require this static ARM32 Dropbear host-key utility in the initramfs")
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
    args = parser.parse_args()

    source, zimage, system_map, ramdisk, boot = map(
        read, (args.source_boot, args.zimage, args.system_map, args.ramdisk, args.boot_image)
    )
    manifest = json.loads(args.manifest.read_text())
    schema_version = manifest_schema(manifest)
    if sha256(source) != SOURCE_SHA256:
        fail("source boot envelope hash mismatch")
    if sha256(zimage) != args.expected_zimage_sha256:
        fail("zImage hash mismatch")
    if sha256(system_map) != args.expected_system_map_sha256:
        fail("System.map hash mismatch")
    if manifest["inputs"].get("system_map", {}).get("sha256") != args.expected_system_map_sha256:
        fail("manifest System.map identity mismatch")
    if sha256(ramdisk) != manifest["initramfs"]["gzip_sha256"]:
        fail("ramdisk hash differs from manifest")
    if sha256(boot) != args.expected_boot_sha256 or manifest["output"]["sha256"] != args.expected_boot_sha256:
        fail("boot-image hash mismatch")
    if manifest.get("status") != "PREPARED_NOT_FLASHED":
        fail("manifest deployment status changed")

    if len(boot) != IMAGE_SIZE or boot[:8] != ANDROID_MAGIC:
        fail("boot image is not the 16 MiB Android v0 envelope")
    source_fields = struct.unpack_from("<10I", source, 8)
    fields = struct.unpack_from("<10I", boot, 8)
    kernel_size, kernel_addr, ramdisk_size, ramdisk_addr = fields[:4]
    second_size, second_addr, tags_addr, page_size, dt_size, unused = fields[4:]
    if (kernel_addr, ramdisk_addr, second_size, second_addr, tags_addr, page_size, dt_size, unused) != (
        KERNEL_ADDR, RAMDISK_ADDR, 0, source_fields[5], TAGS_ADDR, PAGE, 0, source_fields[9]
    ):
        fail("Android header address/geometry contract mismatch")
    if not boot[64:576].startswith(b"bootopt=64S3,32N2,32N2"):
        fail("bootopt no longer selects the proven 32-bit path")
    source_header = bytearray(source[:PAGE])
    output_header = bytearray(boot[:PAGE])
    for start, end in ((8, 12), (16, 24), (576, 608)):
        source_header[start:end] = b"\0" * (end - start)
        output_header[start:end] = b"\0" * (end - start)
    if source_header != output_header:
        fail("Android header changed outside allowed fields")

    kernel = boot[PAGE:PAGE + kernel_size]
    source_kernel = source[PAGE:PAGE + source_fields[0]]
    validate_mkimg_header(kernel)
    source_mkimg, output_mkimg = bytearray(source_kernel[:MKIMG_SIZE]), bytearray(kernel[:MKIMG_SIZE])
    source_mkimg[4:8] = output_mkimg[4:8] = b"\0" * 4
    if source_mkimg != output_mkimg:
        fail("MediaTek KERNEL header changed outside payload size")
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
    stock_dtb = manifest["inputs"]["dtb_origin"] == "stock-envelope-extraction"
    expected_dtb = STOCK_DTB_SHA256 if stock_dtb else args.expected_dtb_sha256
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
    if stock_dtb and sha256(dtb) != PADDED_STOCK_DTB_SHA256:
        fail("stock EVT padded-DTB identity mismatch")

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
        args.expected_bootctl_sha256, args.expected_update_verifier_sha256,
        args.expected_ota_public_key_sha256, args.expected_audio_probe_sha256,
        args.expected_tinyplay_sha256, args.expected_tinycap_sha256,
        args.expected_tinymix_sha256, args.expected_startup_audio_sha256,
        args.expected_iwconfig_sha256,
        args.expected_dropbear_sha256, args.expected_dropbearkey_sha256,
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
    print(
        "arm32_recovery_image_contract=PASS android_v0=yes mtk_wrapper=yes "
        "zimage=yes evt_dtb=yes initramfs_arm32=yes "
        f"fastboot_marker={'automatic' if args.expected_image_profile == 'development' else 'explicit-only'} "
        f"image_profile={args.expected_image_profile} ota=yes "
        "root_adb_staged=yes runme=yes memory_disjoint=yes "
        f"connectivity_bundle={'yes' if connectivity_enabled else 'no'} "
        f"audio_tools={'yes' if args.expected_tinyplay_sha256 and args.expected_tinycap_sha256 and args.expected_tinymix_sha256 else 'no'} "
        f"airplay={'yes' if args.expected_airplay_payload_sha256 or (args.expected_nqptp_sha256 and args.expected_shairport_sync_sha256 and args.expected_avahi_daemon_sha256 and args.expected_dbus_daemon_sha256) else 'no'} "
        f"tts={'yes' if args.expected_tts_payload_sha256 else 'no'} "
        f"wakeword={'yes' if args.expected_wakeword_payload_sha256 else 'no'} "
        f"stt={'yes' if args.expected_stt_payload_sha256 else 'no'} "
        f"assistant={'yes' if args.expected_assistant_payload_sha256 else 'no'} "
        f"network_activation={network_activation} status=PREPARED_NOT_FLASHED"
    )


if __name__ == "__main__":
    main()
