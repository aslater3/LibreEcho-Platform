#!/usr/bin/env python3
"""Build the fail-safe MT8163 ARM32 recovery ramdisk and boot image."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_boot_envelope import generate as generate_boot_envelope


ANDROID_MAGIC = b"ANDROID!"
MKIMG_MAGIC = bytes.fromhex("88168858")
FDT_MAGIC = bytes.fromhex("d00dfeed")
PAGE_SIZE = 0x800
MKIMG_SIZE = 0x200
IMAGE_SIZE = 0x1000000
KERNEL_ADDR = 0x40008000
RAMDISK_ADDR = 0x43478000
RAMDISK_END_LIMIT = 0x44400000
TAGS_ADDR = 0x48000000
ATF_START = 0x43000000
ATF_END = 0x43030000
EVT_SOURCE_OFFSET = 0x585185
EVT_RAW_SIZE = 0xC875
EVT_PADDED_SIZE = 0x10000
ZIMAGE_MAGIC = 0x016F2818

STOCK_EVT_SHA256 = "f44630ba28f503dd7503bc7cffa2ee96a319acf2f58f1456bb6f5ff23d57dee1"
RECOVERY_INIT_SHA256 = "6c3a78d9615d820033876cdc166fbb467d93dda05728bcb5d26c6ad29f074c9b"
BOOT_ENVELOPE_SHA256 = "e83e11b9ef8338cf3262144870790d2b005df16baf4d119849658943e64bbf7a"
PROVEN_ZIMAGE_SHA256 = "4e144959eb0ffaee91b37d05a0f871863a74f4abb1bad0474c2fec358d5176a6"
PROVEN_SYSTEM_MAP_SHA256 = "527292112edd28e8facf2998eefe2224b08a05b193efc73634cd998e9113ba95"
CONNECTIVITY_BUNDLE_ID = "mt8163-v181-stock-v1"
CONNECTIVITY_IMPORTER_SHA256 = "27f20efb39825333838df76eb843e4af537864f326a9648702739286a25e5d3a"
WPA_SUPPLICANT_VERSION = "2.10"
WPA_SOURCE_SHA256 = "20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f"
WPA_SOURCE_URL = "https://w1.fi/releases/wpa_supplicant-2.10.tar.gz"
WIRELESS_TOOLS_VERSION = "30~pre9"
WIRELESS_TOOLS_SOURCE_SHA256 = "abd9c5c98abf1fdd11892ac2f8a56737544fe101e1be27c6241a564948f34c63"
WIRELESS_TOOLS_SOURCE_URL = "https://archive.ubuntu.com/ubuntu/pool/main/w/wireless-tools/wireless-tools_30~pre9.orig.tar.gz"
SSH_PASSWORD_HASH_RE = re.compile(
    r"\$(?:1|5|6|2[abxy]?|y|gy)\$[^$:\r\n]{1,64}\$[^:\r\n]{1,512}\Z"
)

CONNECTIVITY_ASSET_REQUIREMENTS = {
    "ROMv2_lm_patch_1_0_hdr.bin": {
        "source": "system/vendor/firmware/ROMv2_lm_patch_1_0_hdr.bin", "mode": 0o644,
        "size": 128720,
        "sha256": "b4460117f51a43f3284594ec08d8c8861ecc0e42b17820987da03ecabdebac1e",
    },
    "ROMv2_lm_patch_1_1_hdr.bin": {
        "source": "system/vendor/firmware/ROMv2_lm_patch_1_1_hdr.bin", "mode": 0o644,
        "size": 50148,
        "sha256": "10c4ed22a10b8a136bffd7ffce4d552300d76f8e593627d2a9841c3b11a5697e",
    },
    "WIFI_RAM_CODE_8163": {
        "source": "system/vendor/firmware/WIFI_RAM_CODE_8163", "mode": 0o644,
        "size": 373840,
        "sha256": "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c",
    },
    "WMT_SOC.cfg": {
        "source": "system/vendor/firmware/WMT_SOC.cfg", "mode": 0o644, "size": 119,
        "sha256": "302bd4462de99c028c04092e561c1500d65582ce42a93c4c72ccae6e2c99013d",
    },
}

CONNECTIVITY_HELPERS = {
    "sbin/wmt_configure": (
        "wmt_config_helper", 25744,
        "2a57272037a34519e9f6f5dd64ab5a16ad304c81535c4aa7f15a8afae34aadb1",
    ),
    "sbin/wmt_responder": (
        "wmt_responder", 21648,
        "46170ddc1d1ddf21a85ec16df129aac47a258a439bc9e6ed061d1e5942aa48eb",
    ),
    "sbin/wmt_bt_on": (
        "wmt_bt_on", 21648,
        "985320b270149cd27bc59d7f34d0da829817f225a4e712037633517c843cc745",
    ),
    "sbin/wmt_stock_compat": (
        "wmt_stock_compat", 21648,
        "7e3afe31b706029ebf6e271f5cda6e3880cfc5b184abb052a190662759708c87",
    ),
    "sbin/wmt_launcher": (
        "wmt_launcher", 21648,
        "65cb5c0c49bb61aec657c114cf67269e398bf41ff7b70a4abb8eb0ec36ff2c99",
    ),
}

CONNECTIVITY_RUNTIME_SYMLINKS = {
    "etc/firmware": "../lib/firmware",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read {path}: {exc}") from exc


def require_hash(label: str, data: bytes, expected: str) -> None:
    actual = sha256(data)
    if actual != expected:
        raise SystemExit(f"ERROR: {label} SHA-256 mismatch\nexpected={expected}\nactual={actual}")


def align(value: int, size: int = PAGE_SIZE) -> int:
    return (value + size - 1) & ~(size - 1)


def parse_int(value: str) -> int:
    return int(value, 0)


def android_id(kernel: bytes, ramdisk: bytes, second: bytes, dt: bytes) -> bytes:
    digest = hashlib.sha1()
    for blob in (kernel, ramdisk, second):
        digest.update(blob)
        digest.update(struct.pack("<I", len(blob)))
    if dt:
        digest.update(dt)
        digest.update(struct.pack("<I", len(dt)))
    return digest.digest().ljust(32, b"\0")


def elf_identity(path: Path) -> tuple[int, int] | None:
    data = read(path)
    if data[:4] != b"\x7fELF":
        return None
    if len(data) < 20:
        raise SystemExit(f"ERROR: truncated ELF file: {path}")
    byte_order = "<" if data[5] == 1 else ">"
    return data[4], struct.unpack_from(byte_order + "H", data, 18)[0]


def readelf_contract(path: Path) -> tuple[int, str | None, tuple[str, ...], bool]:
    try:
        output = subprocess.run(
            ["readelf", "-h", "-l", "-d", str(path)], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"ERROR: cannot inspect ELF contract for {path}: {exc}") from exc
    if "Class:                             ELF32" not in output:
        raise SystemExit(f"ERROR: {path} is not ELF32")
    if "Data:                              2's complement, little endian" not in output:
        raise SystemExit(f"ERROR: {path} is not little-endian ELF")
    if "Machine:                           ARM" not in output:
        raise SystemExit(f"ERROR: {path} is not an ARM ELF")
    flags_match = re.search(r"^\s*Flags:\s+(0x[0-9a-fA-F]+)", output, re.MULTILINE)
    if flags_match is None:
        raise SystemExit(f"ERROR: readelf did not report ARM ABI flags for {path}")
    interpreter_match = re.search(r"\[Requesting program interpreter: ([^]]+)\]", output)
    interpreter = interpreter_match.group(1) if interpreter_match else None
    needed = tuple(re.findall(r"\(NEEDED\).*Shared library: \[([^]]+)\]", output))
    dynamic = re.search(r"^\s*DYNAMIC\s", output, re.MULTILINE) is not None
    return int(flags_match.group(1), 16), interpreter, needed, dynamic


def require_elf_contract(path: Path, flags: int, interpreter: str | None,
                         needed: tuple[str, ...], dynamic: bool) -> dict[str, object]:
    ident = elf_identity(path)
    if ident != (1, 40):
        raise SystemExit(f"ERROR: ELF identity mismatch for {path}: {ident}")
    actual = readelf_contract(path)
    expected = (flags, interpreter, needed, dynamic)
    if actual != expected:
        raise SystemExit(f"ERROR: ELF ABI/dependency mismatch for {path}: expected={expected!r} actual={actual!r}")
    return {
        "class": 1,
        "machine": 40,
        "flags": f"0x{flags:08x}",
        "interpreter": interpreter,
        "needed": list(needed),
        "dynamic": dynamic,
    }


def pinned_source(root: Path, relative: str, label: str) -> Path:
    relative_path = Path(relative)
    components = relative.split("/")
    if (
        not relative
        or relative_path.is_absolute()
        or any(part in ("", ".", "..") for part in components)
        or relative_path.as_posix() != relative
    ):
        raise SystemExit(f"ERROR: unsafe pinned source path for {label}: {relative!r}")
    source = root
    for part in relative_path.parts:
        source /= part
        if source.is_symlink():
            raise SystemExit(f"ERROR: symlink in pinned source path for {label}: {source}")
    if not source.is_file():
        raise SystemExit(f"ERROR: pinned source is not a regular file for {label}: {source}")
    return source


def copy_adbd(adbd: Path, metadata_path: Path, stage: Path,
              manifest: dict[str, object]) -> None:
    if adbd.is_symlink() or not adbd.is_file():
        raise SystemExit(f"ERROR: adbd is not a regular file: {adbd}")
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise SystemExit(f"ERROR: adbd source metadata is not a regular file: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid adbd source metadata: {exc}") from exc
    required_metadata = {
        "source", "source_url", "source_commit", "source_license", "patch_sha256",
        "compiler", "kernel_headers", "binary_sha256", "binary_size", "transport", "tcp_listener",
    }
    if set(metadata) != required_metadata:
        raise SystemExit("ERROR: adbd source metadata schema mismatch")
    data = read(adbd)
    expected = metadata["binary_sha256"]
    if not isinstance(expected, str) or sha256(data) != expected:
        raise SystemExit("ERROR: adbd source metadata binary hash mismatch")
    if metadata["binary_size"] != len(data):
        raise SystemExit("ERROR: adbd source metadata binary size mismatch")
    if metadata["source_license"] != "Apache-2.0":
        raise SystemExit("ERROR: adbd source license is not Apache-2.0")
    if not isinstance(metadata["kernel_headers"], str) or not metadata["kernel_headers"]:
        raise SystemExit("ERROR: adbd kernel-header provenance is missing")
    if metadata["transport"] != "usb-functionfs-only" or metadata["tcp_listener"] is not False:
        raise SystemExit("ERROR: adbd transport policy is not USB FunctionFS-only")
    target = stage / "sbin/adbd"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o750)
    manifest["adbd"] = {
        "path": "/sbin/adbd",
        "sha256": expected,
        "size": len(data),
        "mode": "0750",
        "source": metadata,
    }


def add_connectivity_runtime_symlinks(stage: Path) -> dict[str, str]:
    symlinks: dict[str, str] = {}
    for relative, link_target in CONNECTIVITY_RUNTIME_SYMLINKS.items():
        target = stage / relative
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: connectivity runtime symlink collides with {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target, target)
        try:
            target.resolve(strict=True).relative_to(stage.resolve())
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"ERROR: connectivity runtime symlink escapes or dangles: "
                f"{relative} -> {link_target}"
            ) from exc
        symlinks[relative] = link_target
    return symlinks


def add_connectivity_bundle(stage: Path, helpers: dict[str, Path],
                            manifest: dict[str, object]) -> None:
    specification_path = stage / f"etc/libreecho/vendor-assets/{CONNECTIVITY_BUNDLE_ID}.tsv"
    importer_path = stage / "usr/local/sbin/libreecho-vendor-import"
    if not specification_path.is_file() or specification_path.is_symlink():
        raise SystemExit("ERROR: local vendor-asset specification is missing")
    if not importer_path.is_file() or importer_path.is_symlink():
        raise SystemExit("ERROR: local vendor-asset importer is missing")
    importer_data = read(importer_path)
    require_hash(
        "local vendor-asset importer", importer_data, CONNECTIVITY_IMPORTER_SHA256
    )

    expected_lines = []
    requirement_records: dict[str, object] = {}
    for target_name, specification in CONNECTIVITY_ASSET_REQUIREMENTS.items():
        expected_hash = str(specification["sha256"])
        expected_size = int(specification["size"])
        source_name = str(specification["source"])
        expected_lines.append(
            f"{expected_hash}|{expected_size}|{source_name}|{target_name}\n"
        )
        requirement_records[target_name] = {
            "source": source_name,
            "sha256": expected_hash,
            "size": expected_size,
            "mode": "0600",
            "persistent_path": f"/data/libreecho/vendor/{CONNECTIVITY_BUNDLE_ID}/{target_name}",
            "runtime_path": f"/lib/firmware/{target_name}",
        }
    specification_data = read(specification_path)
    if specification_data != "".join(expected_lines).encode():
        raise SystemExit("ERROR: local vendor-asset specification changed")

    helper_records: dict[str, object] = {}
    for target_name, (argument_name, expected_size, expected_hash) in CONNECTIVITY_HELPERS.items():
        source = helpers[argument_name]
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"ERROR: connectivity helper is not a regular file: {source}")
        data = read(source)
        if len(data) != expected_size:
            raise SystemExit(
                f"ERROR: connectivity helper {argument_name} size mismatch: "
                f"expected={expected_size} actual={len(data)}"
            )
        require_hash(f"connectivity helper {argument_name}", data, expected_hash)
        target = stage / target_name
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: connectivity helper collides with {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755)
        helper_records[target_name] = {
            "sha256": expected_hash,
            "size": expected_size,
            "mode": "0755",
            "elf": require_elf_contract(target, 0x05000400, None, (), False),
        }

    runtime_symlinks = add_connectivity_runtime_symlinks(stage)
    manifest["connectivity"] = {
        "id": CONNECTIVITY_BUNDLE_ID,
        "enabled": True,
        "activation": "manual-gates-only",
        "autostart": False,
        "vendor_delivery": "owner-device-local-extraction",
        "source_partition": "system_a-read-only",
        "embedded_vendor_file_count": 0,
        "required_vendor_file_count": len(requirement_records),
        "required_vendor_bytes": 552827,
        "helper_count": len(helper_records),
        "payload_bytes": sum(int(record["size"]) for record in helper_records.values()),
        "files": {},
        "required_vendor_assets": requirement_records,
        "importer": {
            "path": "/usr/local/sbin/libreecho-vendor-import",
            "sha256": CONNECTIVITY_IMPORTER_SHA256,
            "size": len(importer_data),
            "mode": "0755",
        },
        "requirements_manifest": {
            "path": f"/etc/libreecho/vendor-assets/{CONNECTIVITY_BUNDLE_ID}.tsv",
            "sha256": sha256(specification_data),
            "size": len(specification_data),
            "mode": "0644",
        },
        "helpers": helper_records,
        "symlinks": runtime_symlinks,
    }


def add_overlay(stage: Path, overlay: Path, busybox: Path, loader: Path,
                expected_busybox_sha256: str, expected_loader_sha256: str,
                qemu_arm: str,
                manifest: dict[str, object]) -> None:
    directories = (
        "bin", "dev", "dev/pts", "dev/socket", "dev/usb-ffs", "dev/usb-ffs/adb",
        "etc", "etc/wifi", "lib", "lib/firmware", "proc", "sbin", "sys", "system", "system/bin", "tmp",
    )
    for directory in directories:
        target = stage / directory
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o777 if directory == "tmp" else 0o755)

    overlay_files = {
        "default.prop": ("default.prop", 0o644),
        "profile": ("etc/profile", 0o644),
        "init.rc": ("init.rc", 0o644),
        "init.recovery.mt8163.rc": ("init.recovery.mt8163.rc", 0o644),
        "libreecho-init": ("libreecho-init", 0o755),
        "libreecho-data-cleanup": (
            "usr/local/sbin/libreecho-data-cleanup", 0o755,
        ),
        "libreecho-vendor-import": (
            "usr/local/sbin/libreecho-vendor-import", 0o755,
        ),
        "vendor-assets/mt8163-v181-stock-v1.tsv": (
            "etc/libreecho/vendor-assets/mt8163-v181-stock-v1.tsv", 0o644,
        ),
        "libreecho-update": ("usr/local/sbin/libreecho-update", 0o755),
        "libreecho-update-fetch": ("usr/local/sbin/libreecho-update-fetch", 0o755),
        "ota-source.conf": ("etc/libreecho/ota-source.conf", 0o644),
        "libreecho-wifi": ("sbin/libreecho-wifi", 0o755),
        "udhcpc.script": ("etc/udhcpc.script", 0o755),
        "wpa_supplicant.conf.example": (
            "etc/wifi/wpa_supplicant.conf.example", 0o600,
        ),
        "regulatory.db": ("lib/firmware/regulatory.db", 0o644),
        "regulatory.db.p7s": ("lib/firmware/regulatory.db.p7s", 0o644),
    }
    overlay_manifest: dict[str, object] = {}
    for relative, (target_relative, mode) in overlay_files.items():
        data = read(overlay / relative)
        target = stage / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        overlay_manifest[relative] = {"sha256": sha256(data), "size": len(data), "mode": f"{mode:04o}"}

    core_license_root = overlay / "usr/local/share/licenses/libreecho-core"
    core_license_files = sorted(path for path in core_license_root.rglob("*") if path.is_file())
    if not core_license_files:
        raise SystemExit("ERROR: LibreEcho core license bundle is empty")
    for source in core_license_files:
        if source.is_symlink():
            raise SystemExit(f"ERROR: core license input is a symlink: {source}")
        relative = source.relative_to(overlay).as_posix()
        data = read(source)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
        overlay_manifest[relative] = {
            "sha256": sha256(data), "size": len(data), "mode": "0644",
        }

    # The stock ramdisk's /init is an Android ELF that is incompatible with
    # this ARM32 recovery kernel.  PID 1 must be the audited LibreEcho shell
    # control script, installed at the real runtime path (not merely staged
    # as /libreecho-init).
    init_script = read(overlay / "libreecho-init")
    require_hash("LibreEcho recovery /init", init_script, RECOVERY_INIT_SHA256)
    init_target = stage / "init"
    init_target.write_bytes(init_script)
    init_target.chmod(0o755)
    overlay_manifest["init"] = {
        "sha256": RECOVERY_INIT_SHA256,
        "size": len(init_script),
        "mode": "0755",
        "source": "libreecho-init",
    }

    busybox_data = read(busybox)
    loader_data = read(loader)
    require_hash("ARM32 BusyBox", busybox_data, expected_busybox_sha256)
    require_hash("ARM32 musl loader", loader_data, expected_loader_sha256)
    (stage / "bin/busybox").write_bytes(busybox_data)
    (stage / "bin/busybox").chmod(0o755)
    (stage / "lib/ld-musl-armhf.so.1").write_bytes(loader_data)
    (stage / "lib/ld-musl-armhf.so.1").chmod(0o755)

    fixed_links = {
        "lib/libc.musl-armv7.so.1": "ld-musl-armhf.so.1",
        "sbin/sh": "../bin/busybox",
        "sbin/ueventd": "../init",
        "sbin/watchdogd": "../init",
        "system/bin/sh": "../../bin/busybox",
    }
    for relative, target in fixed_links.items():
        os.symlink(target, stage / relative)

    applet_output = subprocess.run(
        [qemu_arm, "-L", str(stage), str(stage / "bin/busybox"), "--list"],
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    applets = sorted(set(applet_output.splitlines()))
    if len(applets) < 250:
        raise SystemExit(f"ERROR: BusyBox applet inventory is unexpectedly short: {len(applets)}")
    for applet in applets:
        if not applet or "/" in applet or applet in {".", "..", "busybox"}:
            raise SystemExit(f"ERROR: unsafe BusyBox applet name {applet!r}")
        target = stage / "bin" / applet
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: BusyBox applet collides with {target}")
        os.symlink("busybox", target)

    manifest["overlay"] = overlay_manifest
    manifest["busybox"] = {"sha256": expected_busybox_sha256, "size": len(busybox_data)}
    manifest["musl_loader"] = {"sha256": expected_loader_sha256, "size": len(loader_data)}
    manifest["symlinks"] = fixed_links
    manifest["busybox_applets"] = {"count": len(applets), "names": applets}


def add_ota_tools(stage: Path, bootctl: Path, verifier: Path, public_key: Path,
                  image_profile: str, service_profile: str, feature_policy: str,
                  update_channel: str, manifest: dict[str, object]) -> None:
    sources = (
        ("bootctl", bootctl, "usr/local/sbin/libreecho-bootctl",
         "/lib/ld-musl-armhf.so.1", ("libc.musl-armv7.so.1",), True),
        ("verifier", verifier, "usr/local/libexec/libreecho-update-verify",
         None, (), False),
    )
    records: dict[str, object] = {}
    for name, source, relative, interpreter, needed, dynamic in sources:
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"ERROR: OTA {name} is not a regular file: {source}")
        data = read(source)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755)
        records[name] = {
            "sha256": sha256(data),
            "size": len(data),
            "path": "/" + relative,
            "elf": require_elf_contract(
                target, 0x05000400, interpreter, needed, dynamic,
            ),
        }

    key_data = read(public_key)
    if not re.fullmatch(rb"[0-9a-f]{64}\n", key_data):
        raise SystemExit("ERROR: OTA Ed25519 public key must be 32-byte lowercase hex")
    key_target = stage / "etc/libreecho/ota-public-key.hex"
    key_target.parent.mkdir(parents=True, exist_ok=True)
    key_target.write_bytes(key_data)
    key_target.chmod(0o644)

    profile_target = stage / "etc/libreecho/image-profile"
    profile_target.write_text(image_profile + "\n")
    profile_target.chmod(0o644)
    service_profile_target = stage / "etc/libreecho/service-profile"
    service_profile_target.write_text(service_profile + "\n")
    service_profile_target.chmod(0o644)
    feature_policy_target = stage / "etc/libreecho/feature-policy"
    feature_policy_target.write_text(feature_policy + "\n")
    feature_policy_target.chmod(0o644)
    update_channel_target = stage / "etc/libreecho/update-channel"
    update_channel_target.write_text(update_channel + "\n")
    update_channel_target.chmod(0o644)
    first_install_target = stage / "etc/libreecho/first-install-confirm"
    first_install_target.write_text(
        "schema=1\nmode=first-install\nboard=radar_puffin\n"
    )
    first_install_target.chmod(0o644)
    ota_source = stage / "etc/libreecho/ota-source.conf"
    if ota_source.is_file() and not ota_source.is_symlink():
        source_text = ota_source.read_text()
        source_text = re.sub(
            r"^channel=.*$", f"channel={update_channel}", source_text,
            count=1, flags=re.MULTILINE,
        )
        source_text = re.sub(
            r"libreecho-radar-puffin-(?:dev|stable)\.ota\.tar",
            f"libreecho-radar-puffin-{update_channel}.ota.tar", source_text,
        )
        ota_source.write_text(source_text)
        overlay_manifest = manifest.get("overlay")
        if not isinstance(overlay_manifest, dict):
            raise SystemExit("ERROR: OTA source rewrite requires overlay manifest")
        source_data = source_text.encode()
        overlay_manifest["ota-source.conf"] = {
            "sha256": sha256(source_data), "size": len(source_data), "mode": "0644",
        }
    manifest["image_profile"] = image_profile
    manifest["service_profile"] = service_profile
    manifest["feature_policy"] = feature_policy
    manifest["update_channel"] = update_channel
    manifest["ota"] = {
        "enabled": True,
        "format": "libreecho-ota-v1",
        "board": "radar_puffin",
        "payload_slots": {"a": "mmcblk0p10", "b": "mmcblk0p11"},
        "wrapper_partitions": ["mmcblk0p17", "mmcblk0p18"],
        "bcb": {"partition": "mmcblk0p8", "offset": 0x360, "record_size": 7},
        "persistent_configuration": "/data/libreecho/config",
        "public_key_sha256": sha256(key_data),
        "tools": records,
    }


def add_audio_probe(stage: Path, audio_probe: Path,
                    manifest: dict[str, object]) -> None:
    """Install the dependency-free ARM32 ALSA capability probe."""
    if audio_probe.is_symlink() or not audio_probe.is_file():
        raise SystemExit(f"ERROR: audio probe is not a regular file: {audio_probe}")
    data = read(audio_probe)
    target = stage / "sbin/audio_probe"
    if target.exists() or target.is_symlink():
        raise SystemExit(f"ERROR: audio probe collides with {target}")
    target.write_bytes(data)
    target.chmod(0o755)
    manifest["audio"] = {
        "enabled": True,
        "activation": "manual-only",
        "probe": {
            "path": str(audio_probe.resolve()),
            "sha256": sha256(data),
            "size": len(data),
            "mode": "0755",
            "elf": require_elf_contract(target, 0x05000400, None, (), False),
        },
    }


def add_audio_tools(stage: Path, tinyplay: Path, tinycap: Path, tinymix: Path,
                    manifest: dict[str, object]) -> None:
    """Install the patched static TinyALSA playback/capture/mixer utilities."""
    audio = manifest.get("audio")
    if not isinstance(audio, dict) or not audio.get("enabled"):
        raise SystemExit("ERROR: audio tools require the audio probe")

    tools: dict[str, object] = {}
    for name, source, target_name in (
        ("tinyplay", tinyplay, "sbin/tinyplay"),
        ("tinycap", tinycap, "sbin/tinycap"),
        ("tinymix", tinymix, "sbin/tinymix"),
    ):
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"ERROR: {name} is not a regular file: {source}")
        data = read(source)
        target = stage / target_name
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: audio tool collides with {target}")
        target.write_bytes(data)
        target.chmod(0o755)
        tools[name] = {
            "path": str(source.resolve()),
            "sha256": sha256(data),
            "size": len(data),
            "mode": "0755",
            "elf": require_elf_contract(target, 0x05000400, None, (), False),
        }
    audio["tools"] = tools


def add_network_tools(stage: Path, iwconfig: Path, iwconfig_metadata_path: Path,
                      manifest: dict[str, object]) -> None:
    """Install the manual network inspection tools.

    ifconfig is provided by the already-pinned BusyBox applet set.  Expose a
    conventional /sbin path for it and pair it with a separately pinned,
    static wireless-tools iwconfig binary.
    """
    ifconfig = stage / "bin/ifconfig"
    if not ifconfig.is_symlink() or os.readlink(ifconfig) != "busybox":
        raise SystemExit("ERROR: BusyBox ifconfig applet is missing or changed")
    ifconfig_target = stage / "sbin/ifconfig"
    if ifconfig_target.exists() or ifconfig_target.is_symlink():
        raise SystemExit(f"ERROR: network tool collides with {ifconfig_target}")
    os.symlink("../bin/ifconfig", ifconfig_target)

    if iwconfig.is_symlink() or not iwconfig.is_file():
        raise SystemExit(f"ERROR: iwconfig is not a regular file: {iwconfig}")
    if iwconfig_metadata_path.is_symlink() or not iwconfig_metadata_path.is_file():
        raise SystemExit(f"ERROR: wireless-tools source metadata is not a regular file: {iwconfig_metadata_path}")
    iwconfig_data = read(iwconfig)
    try:
        iwconfig_metadata = json.loads(iwconfig_metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid wireless-tools source metadata: {exc}") from exc
    required_metadata = {
        "binary_sha256", "binary_size", "build_epoch", "compiler",
        "kernel_uapi_sha256", "license", "license_file", "license_sha256",
        "source_sha256", "source_url", "static", "version",
    }
    if not isinstance(iwconfig_metadata, dict) or set(iwconfig_metadata) != required_metadata:
        raise SystemExit("ERROR: wireless-tools source metadata schema mismatch")
    if (iwconfig_metadata["binary_sha256"] != sha256(iwconfig_data) or
            iwconfig_metadata["binary_size"] != len(iwconfig_data)):
        raise SystemExit("ERROR: wireless-tools source metadata binary identity mismatch")
    if iwconfig_metadata["license_file"] != "wireless-tools-COPYING":
        raise SystemExit("ERROR: wireless-tools license file identity is invalid")
    license_data = read(iwconfig_metadata_path.parent / "wireless-tools-COPYING")
    if (iwconfig_metadata["license_sha256"] != sha256(license_data) or
            iwconfig_metadata["source_sha256"] != WIRELESS_TOOLS_SOURCE_SHA256 or
            iwconfig_metadata["source_url"] != WIRELESS_TOOLS_SOURCE_URL or
            iwconfig_metadata["license"] != "GPL-2.0-only AND LGPL-2.1-or-later" or
            iwconfig_metadata["version"] != WIRELESS_TOOLS_VERSION or
            iwconfig_metadata["static"] is not True or
            not isinstance(iwconfig_metadata["kernel_uapi_sha256"], str) or
            not re.fullmatch(r"[0-9a-f]{64}", iwconfig_metadata["kernel_uapi_sha256"])):
        raise SystemExit("ERROR: wireless-tools source metadata provenance is invalid")
    license_target = stage / "usr/local/share/licenses/libreecho-core/wireless-tools-COPYING"
    if license_target.exists() or license_target.is_symlink():
        raise SystemExit(f"ERROR: wireless-tools license collides with {license_target}")
    license_target.write_bytes(license_data)
    license_target.chmod(0o644)
    iwconfig_target = stage / "sbin/iwconfig"
    if iwconfig_target.exists() or iwconfig_target.is_symlink():
        raise SystemExit(f"ERROR: network tool collides with {iwconfig_target}")
    iwconfig_target.write_bytes(iwconfig_data)
    iwconfig_target.chmod(0o755)
    iwconfig_elf = require_elf_contract(iwconfig_target, 0x05000400, None, (), False)

    manifest["network_tools"] = {
        "enabled": True,
        "activation": "manual-only",
        "autostart": False,
        "tools": {
            "ifconfig": {
                "path": "/sbin/ifconfig",
                "provider": "busybox",
                "target": "../bin/ifconfig",
                "mode": "0777",
            },
            "iwconfig": {
                "path": str(iwconfig.resolve()),
                "sha256": sha256(iwconfig_data),
                "size": len(iwconfig_data),
                "mode": "0755",
                "elf": iwconfig_elf,
                "source": iwconfig_metadata,
            },
        },
    }


def validate_ui_startup_contract(bundle: Path) -> None:
    """Reject UI bundles that cannot drive the packaged startup hand-off."""
    contracts = (
        (
            "etc/init.d/libreecho-ledd.init",
            (
                "--startup-animation",
                "--startup-ready $STARTUP_READY",
                'start-stop-daemon -S -b -m -p "$PIDFILE" -x "$DAEMON" -- $ARGS',
                "start) start_service",
            ),
        ),
        (
            "etc/init.d/libreecho-web.init",
            (
                "startup_services_ready()",
                "for socket in network audio mic led bluetooth airplay; do",
                "mark_startup_ready()",
                'tmp="$STARTUP_READY.tmp"',
                "printf 'schema=1\\n' >\"$tmp\"",
                'mv -f "$tmp" "$STARTUP_READY"',
                "if startup_services_ready; then",
                "for socket in network audio mic led bluetooth airplay; do",
                '[ -S "/run/libreecho/$socket.sock" ] || return 1',
                "start_service\n        mark_startup_ready >/dev/null 2>&1 &",
            ),
        ),
    )
    for relative, required in contracts:
        source = pinned_source(bundle, relative, f"UI startup contract {relative}")
        try:
            text = read(source).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(
                f"ERROR: UI startup contract is not UTF-8: {relative}"
            ) from exc
        syntax = subprocess.run(
            ["sh", "-n", str(source)], capture_output=True, text=True,
        )
        if syntax.returncode != 0:
            raise SystemExit(
                f"ERROR: UI startup contract has invalid shell syntax: {relative}"
            )
        for marker in required:
            if marker not in text:
                raise SystemExit(
                    f"ERROR: UI startup contract missing {marker!r} in {relative}"
                )


def add_ui_bundle(stage: Path, bundle: Path, source: Path,
                  expected_commit: str, expected_diff_sha256: str,
                  manifest: dict[str, object]) -> None:
    """Install the separately-built UI as a manual image entry point."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise SystemExit(f"ERROR: UI bundle is not a directory: {bundle}")
    if source.is_symlink() or not source.is_dir():
        raise SystemExit(f"ERROR: UI source is not a directory: {source}")

    manifest_source = pinned_source(
        bundle, "share/libreecho/ui-manifest.txt", "UI file manifest",
    )
    manifest_data = read(manifest_source)
    try:
        manifest_lines = manifest_data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise SystemExit("ERROR: UI file manifest is not ASCII") from exc
    if f"source_commit={expected_commit}" not in manifest_lines:
        raise SystemExit("ERROR: UI source commit does not match the requested pin")
    if f"source_diff_sha256={expected_diff_sha256}" not in manifest_lines:
        raise SystemExit("ERROR: UI source diff identity does not match the requested pin")

    bundled_files: dict[str, str] = {}
    for line in manifest_lines:
        if not line.startswith("file="):
            continue
        fields = line.split()
        if len(fields) != 2 or not fields[0].startswith("file=") or not fields[1].startswith("sha256="):
            raise SystemExit(f"ERROR: malformed UI file manifest line: {line!r}")
        relative = fields[0][len("file="):]
        digest = fields[1][len("sha256="):]
        if not relative or relative in bundled_files or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"ERROR: invalid UI file manifest entry: {line!r}")
        source_file = pinned_source(bundle, relative, f"UI bundle file {relative}")
        actual = sha256(read(source_file))
        if actual != digest:
            raise SystemExit(f"ERROR: UI bundle file hash mismatch: {relative}")
        bundled_files[relative] = digest

    actual_bundle_files = sorted(
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path != manifest_source
    )
    if actual_bundle_files != sorted(bundled_files):
        raise SystemExit("ERROR: UI file manifest does not cover the complete bundle")
    validate_ui_startup_contract(bundle)

    files: dict[str, object] = {}

    def copy_file(relative: str, target_name: str, mode: int,
                  elf: bool = False) -> None:
        source_file = pinned_source(bundle, relative, f"UI file {relative}")
        target = stage / target_name
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: UI file collides with {target}")
        data = read(source_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        record: dict[str, object] = {
            "source": relative,
            "sha256": sha256(data),
            "size": len(data),
            "mode": f"{mode:04o}",
        }
        if elf:
            if target.name in {
                    "libreecho-sttd-wyoming", "libreecho-ttsd-wyoming"}:
                record["elf"] = require_elf_contract(
                    target, 0x05000400, "/lib/ld-musl-armhf.so.1",
                    ("libc.musl-armv7.so.1",), True,
                )
            else:
                record["elf"] = require_elf_contract(
                    target, 0x05000400, None, (), False,
                )
        files[target_name] = record

    for binary in (
        "libreecho-web", "libreecho-logd", "libreecho-networkd",
        "libreecho-timed", "libreecho-audiod", "libreecho-micd",
        "libreecho-ledd", "libreecho-btd",
        "libreecho-airplayd", "libreecho-wyomingd",
        "libreecho-sttd-wyoming", "libreecho-ttsd-wyoming",
    ):
        copy_file(f"sbin/{binary}", f"usr/local/sbin/{binary}", 0o755, True)
    for script in (
        "libreecho-web.init", "libreecho-logd.init", "libreecho-networkd.init",
        "libreecho-timed.init", "libreecho-audiod.init",
        "libreecho-micd.init", "libreecho-ledd.init", "libreecho-btd.init",
        "libreecho-airplayd.init", "libreecho-ttsd.init", "libreecho-waked.init",
        "libreecho-sttd.init", "libreecho-agentd.init", "libreecho-wyomingd.init",
    ):
        copy_file(f"etc/init.d/{script}", f"etc/init.d/{script}", 0o755)
    copy_file("etc/libreecho/web-config.json", "etc/libreecho/web-config.json", 0o600)
    copy_file("etc/libreecho/airplay2.conf", "etc/libreecho/airplay2.conf", 0o644)
    copy_file("etc/libreecho/ntp.conf", "etc/libreecho/ntp.conf", 0o644)
    if "etc/libreecho/users" in bundled_files:
        users_file = pinned_source(bundle, "etc/libreecho/users", "UI users file")
        if users_file.stat().st_mode & 0o077 or not read(users_file).strip():
            raise SystemExit("ERROR: UI users file must be private and non-empty")
        copy_file("etc/libreecho/users", "etc/libreecho/users", 0o600)
    for relative in sorted(bundled_files):
        if not relative.startswith("share/libreecho/web/"):
            continue
        target_name = "usr/local/" + relative
        copy_file(relative, target_name, 0o644)
    copy_file(
        "share/libreecho/ui-manifest.txt",
        "usr/local/share/libreecho/ui-manifest.txt", 0o644,
    )

    manifest["ui"] = {
        "enabled": True,
        "activation": "automatic-after-loopback",
        "autostart": True,
        "hardware_ownership": "existing-control-plane",
        "source": str(source.resolve()),
        "commit": expected_commit,
        "diff_sha256": expected_diff_sha256,
        "manifest_sha256": sha256(manifest_data),
        "files": files,
    }


def add_airplay_bundle(stage: Path, nqptp: Path, shairport_sync: Path,
                       avahi_daemon: Path, dbus_daemon: Path,
                       runtime: Path, manifest: dict[str, object]) -> None:
    """Install the packaged AirPlay 2 payload and its glibc runtime closure."""
    for source, target_name in (
        (nqptp, "usr/local/sbin/nqptp"),
        (shairport_sync, "usr/local/sbin/shairport-sync"),
        (avahi_daemon, "usr/local/sbin/avahi-daemon"),
        (dbus_daemon, "usr/local/sbin/dbus-daemon"),
    ):
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"ERROR: AirPlay binary is not a regular file: {source}")
        target = stage / target_name
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: AirPlay binary collides with {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read(source))
        target.chmod(0o755)

    nqptp_elf = require_elf_contract(
        stage / "usr/local/sbin/nqptp", 0x05000400, None, (), False,
    )
    dynamic_elf: dict[str, tuple[int, str | None, tuple[str, ...], bool]] = {}
    for target_name in (
        "usr/local/sbin/shairport-sync",
        "usr/local/sbin/avahi-daemon",
        "usr/local/sbin/dbus-daemon",
    ):
        info = readelf_contract(stage / target_name)
        if info[0] != 0x05000400 or info[1] != "/lib/ld-linux-armhf.so.3":
            raise SystemExit(f"ERROR: AirPlay ELF contract is not ARMHF glibc: {target_name} {info}")
        if not info[2] or not info[3]:
            raise SystemExit(f"ERROR: AirPlay daemon must be dynamically linked: {target_name}")
        dynamic_elf[target_name] = info
    shairport_elf = dynamic_elf["usr/local/sbin/shairport-sync"]

    if runtime.is_symlink() or not runtime.is_dir():
        raise SystemExit(f"ERROR: AirPlay runtime closure is not a directory: {runtime}")
    runtime_files = sorted(
        path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if path.is_file()
    )
    required_loader = "lib/ld-linux-armhf.so.3"
    if required_loader not in runtime_files:
        raise SystemExit("ERROR: AirPlay runtime closure lacks lib/ld-linux-armhf.so.3")
    if any(
        name != required_loader and
        name not in {
            "etc/avahi/avahi-daemon.conf", "etc/dbus-1/system.conf",
            "etc/dbus-1/system.d/avahi-dbus.conf",
        } and
        (not name.startswith("usr/lib/") or ".so." not in name)
        for name in runtime_files
    ):
        raise SystemExit("ERROR: AirPlay runtime closure contains an unexpected file")

    runtime_records: dict[str, object] = {}
    for relative in runtime_files:
        source = pinned_source(runtime, relative, f"AirPlay runtime {relative}")
        target = stage / relative
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: AirPlay runtime collides with {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read(source))
        mode = 0o644 if relative.startswith("etc/") else 0o755
        target.chmod(mode)
        if relative.startswith("etc/"):
            runtime_records[relative] = {
                "sha256": sha256(read(source)),
                "size": source.stat().st_size,
                "mode": "0644",
            }
            continue
        info = readelf_contract(target)
        if info[0] != 0x05000400:
            raise SystemExit(f"ERROR: AirPlay runtime is not ARMHF: {relative}")
        runtime_records[relative] = {
            "sha256": sha256(read(source)),
            "size": source.stat().st_size,
            "mode": "0755",
            "elf": {
                "flags": f"0x{info[0]:08x}",
                "interpreter": info[1],
                "needed": list(info[2]),
                "dynamic": info[3],
            },
        }

    manifest["airplay"] = {
        "enabled": True,
        "activation": "manual-ui-toggle",
        "autostart": False,
        "protocol": "airplay2",
        "mdns": "avahi",
        "audio_transport": "shairport-pipe-to-shared-priority-engine",
        "tinyalsa_pcm": "hw:0,23",
        "nqptp": {
            "path": str(nqptp.resolve()),
            "sha256": sha256(read(nqptp)),
            "size": nqptp.stat().st_size,
            "mode": "0755",
            "elf": nqptp_elf,
        },
        "shairport_sync": {
            "path": str(shairport_sync.resolve()),
            "sha256": sha256(read(shairport_sync)),
            "size": shairport_sync.stat().st_size,
            "mode": "0755",
            "elf": {
                "flags": f"0x{shairport_elf[0]:08x}",
                "interpreter": shairport_elf[1],
                "needed": list(shairport_elf[2]),
                "dynamic": shairport_elf[3],
            },
        },
        "avahi_daemon": {
            "path": str(avahi_daemon.resolve()),
            "sha256": sha256(read(avahi_daemon)),
            "size": avahi_daemon.stat().st_size,
            "mode": "0755",
            "elf": {
                "flags": f"0x{dynamic_elf['usr/local/sbin/avahi-daemon'][0]:08x}",
                "interpreter": dynamic_elf["usr/local/sbin/avahi-daemon"][1],
                "needed": list(dynamic_elf["usr/local/sbin/avahi-daemon"][2]),
                "dynamic": dynamic_elf["usr/local/sbin/avahi-daemon"][3],
            },
        },
        "dbus_daemon": {
            "path": str(dbus_daemon.resolve()),
            "sha256": sha256(read(dbus_daemon)),
            "size": dbus_daemon.stat().st_size,
            "mode": "0755",
            "elf": {
                "flags": f"0x{dynamic_elf['usr/local/sbin/dbus-daemon'][0]:08x}",
                "interpreter": dynamic_elf["usr/local/sbin/dbus-daemon"][1],
                "needed": list(dynamic_elf["usr/local/sbin/dbus-daemon"][2]),
                "dynamic": dynamic_elf["usr/local/sbin/dbus-daemon"][3],
            },
        },
        "runtime": runtime_records,
    }


def add_airplay_external_payload(payload: Path, payload_manifest: Path,
                                 manifest: dict[str, object]) -> None:
    """Record an AirPlay feature payload without putting its runtime in boot.img."""
    if payload.is_symlink() or not payload.is_file():
        raise SystemExit(f"ERROR: AirPlay feature payload is not a regular file: {payload}")
    if payload_manifest.is_symlink() or not payload_manifest.is_file():
        raise SystemExit(f"ERROR: AirPlay feature manifest is not a regular file: {payload_manifest}")
    try:
        feature = json.loads(payload_manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: AirPlay feature manifest is invalid: {payload_manifest}") from exc
    if (not isinstance(feature, dict) or feature.get("schema_version") != 1 or
            feature.get("feature_id") != "airplay2" or
            feature.get("format") != "squashfs-lz4"):
        raise SystemExit("ERROR: AirPlay feature manifest contract changed")
    feature_payload = feature.get("payload")
    feature_files = feature.get("files")
    if not isinstance(feature_payload, dict) or not isinstance(feature_files, dict):
        raise SystemExit("ERROR: AirPlay feature manifest lacks payload/files records")
    for required in (
            "usr/local/sbin/libreecho-airplay-audio",
            "usr/local/sbin/libreecho-audio-engine",
            "usr/local/sbin/shairport-sync",
            "etc/libreecho/airplay2.conf",
            "usr/local/share/licenses/libreecho-airplay/COMPONENTS.tsv"):
        if required not in feature_files:
            raise SystemExit(f"ERROR: AirPlay feature member missing: {required}")
    if not any(str(relative).startswith(
            "usr/local/share/licenses/libreecho-airplay/debian/") and
            str(relative).endswith("/copyright") for relative in feature_files):
        raise SystemExit("ERROR: AirPlay Debian copyright closure is missing")
    for component in ("nqptp", "shairport-sync", "ffmpeg", "tinyalsa"):
        prefix = f"usr/local/share/licenses/libreecho-airplay/source/{component}/"
        if not any(str(relative).startswith(prefix) for relative in feature_files):
            raise SystemExit(f"ERROR: AirPlay source license closure missing: {component}")
    payload_hash = sha256(read(payload))
    payload_size = payload.stat().st_size
    if (feature_payload.get("filename") != payload.name or
            feature_payload.get("sha256") != payload_hash or
            feature_payload.get("size") != payload_size):
        raise SystemExit("ERROR: AirPlay feature payload does not match its manifest")
    for relative, record in feature_files.items():
        if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                "//" in relative or "/../" in f"/{relative}/" or
                not isinstance(record, dict) or
                not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
            raise SystemExit(f"ERROR: unsafe AirPlay feature file record: {relative!r}")
    manifest["airplay"] = {
        "enabled": True,
        "activation": "manual-ui-toggle",
        "autostart": False,
        "protocol": "airplay2",
        "mdns": "avahi",
        "audio_transport": "shairport-pipe-to-shared-priority-engine",
        "tinyalsa_pcm": "hw:0,23",
        "external_payload": True,
        "payload": {
            "filename": payload.name,
            "sha256": payload_hash,
            "size": payload_size,
            "format": "squashfs-lz4",
            "manifest_sha256": sha256(read(payload_manifest)),
            "files": feature_files,
        },
        "runtime": {},
    }


def add_tts_external_payload(payload: Path, payload_manifest: Path,
                             manifest: dict[str, object]) -> None:
    """Record the persistent two-voice TTS payload without bloating boot.img."""
    if payload.is_symlink() or not payload.is_file():
        raise SystemExit(f"ERROR: TTS feature payload is not a regular file: {payload}")
    if payload_manifest.is_symlink() or not payload_manifest.is_file():
        raise SystemExit(f"ERROR: TTS feature manifest is not a regular file: {payload_manifest}")
    try:
        feature = json.loads(payload_manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: TTS feature manifest is invalid: {payload_manifest}") from exc
    if (not isinstance(feature, dict) or feature.get("schema_version") != 1 or
            feature.get("feature_id") != "tts" or
            feature.get("format") != "squashfs-lz4"):
        raise SystemExit("ERROR: TTS feature manifest contract changed")
    feature_payload = feature.get("payload")
    feature_files = feature.get("files")
    if not isinstance(feature_payload, dict) or not isinstance(feature_files, dict):
        raise SystemExit("ERROR: TTS feature manifest lacks payload/files records")
    required_files = (
        "usr/local/sbin/libreecho-ttsd",
        "usr/local/share/libreecho/tts/models/northern-male/model.onnx",
        "usr/local/share/libreecho/tts/models/northern-male/tokens.txt",
        "usr/local/share/libreecho/tts/models/southern-female/model.onnx",
        "usr/local/share/libreecho/tts/models/southern-female/tokens.txt",
    )
    for required in required_files:
        if required not in feature_files:
            raise SystemExit(f"ERROR: TTS feature member missing: {required}")
    for voice in ("northern-male", "southern-female"):
        prefix = f"usr/local/share/libreecho/tts/models/{voice}/espeak-ng-data/"
        if not any(str(relative).startswith(prefix) for relative in feature_files):
            raise SystemExit(f"ERROR: TTS feature lacks eSpeak English data for {voice}")
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
        if required_notice not in feature_files:
            raise SystemExit(f"ERROR: TTS feature notice missing: {required_notice}")
    payload_hash = sha256(read(payload))
    payload_size = payload.stat().st_size
    if (feature_payload.get("filename") != payload.name or
            feature_payload.get("sha256") != payload_hash or
            feature_payload.get("size") != payload_size):
        raise SystemExit("ERROR: TTS feature payload does not match its manifest")
    for relative, record in feature_files.items():
        if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                "//" in relative or "/../" in f"/{relative}/" or
                not isinstance(record, dict) or
                not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
            raise SystemExit(f"ERROR: unsafe TTS feature file record: {relative!r}")
    manifest["tts"] = {
        "enabled": True,
        "activation": "automatic-after-audio-engine",
        "autostart": True,
        "audio_transport": "audiod-to-streamed-announcement-priority-bus",
        "voices": ["southern-female", "northern-male"],
        "default_voice": "southern-female",
        "threads": 4,
        "streaming": True,
        "in_process": True,
        "cpu_boost_during_synthesis": True,
        "external_payload": True,
        "payload": {
            "filename": payload.name,
            "sha256": payload_hash,
            "size": payload_size,
            "format": "squashfs-lz4",
            "manifest_sha256": sha256(read(payload_manifest)),
            "files": feature_files,
        },
    }


def add_wakeword_external_payload(payload: Path, payload_manifest: Path,
                                  manifest: dict[str, object]) -> None:
    """Record the reduced openWakeWord runtime without bloating boot.img."""
    if payload.is_symlink() or not payload.is_file():
        raise SystemExit(
            f"ERROR: wakeword feature payload is not a regular file: {payload}"
        )
    if payload_manifest.is_symlink() or not payload_manifest.is_file():
        raise SystemExit(
            f"ERROR: wakeword feature manifest is not a regular file: {payload_manifest}"
        )
    try:
        feature = json.loads(payload_manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"ERROR: wakeword feature manifest is invalid: {payload_manifest}"
        ) from exc
    if (not isinstance(feature, dict) or feature.get("schema_version") != 1 or
            feature.get("feature_id") != "wakeword" or
            feature.get("format") != "squashfs-lz4"):
        raise SystemExit("ERROR: wakeword feature manifest contract changed")
    feature_payload = feature.get("payload")
    feature_files = feature.get("files")
    if not isinstance(feature_payload, dict) or not isinstance(feature_files, dict):
        raise SystemExit("ERROR: wakeword feature manifest lacks payload/files records")
    required_files = (
        "usr/local/sbin/libreecho-waked",
        "usr/local/share/libreecho/openwakeword/melspectrogram.onnx",
        "usr/local/share/libreecho/openwakeword/embedding_model.onnx",
        "usr/local/share/libreecho/openwakeword/alexa_v0.1.onnx",
        "usr/local/share/licenses/libreecho-openwakeword/MODEL-LICENSE.txt",
        "usr/local/share/licenses/libreecho-openwakeword/CC-BY-NC-SA-4.0.txt",
        "usr/local/share/licenses/libreecho-openwakeword/runtime/RUNTIME-NOTICES.txt",
        "usr/local/share/licenses/libreecho-openwakeword/runtime/ONNX-Runtime-MIT.txt",
        "usr/local/share/licenses/libreecho-openwakeword/runtime/SpeexDSP-COPYING.txt",
    )
    for required in required_files:
        if required not in feature_files:
            raise SystemExit(f"ERROR: wakeword feature member missing: {required}")
    payload_hash = sha256(read(payload))
    payload_size = payload.stat().st_size
    if (feature_payload.get("filename") != payload.name or
            feature_payload.get("sha256") != payload_hash or
            feature_payload.get("size") != payload_size):
        raise SystemExit("ERROR: wakeword feature payload does not match its manifest")
    for relative, record in feature_files.items():
        if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                "//" in relative or "/../" in f"/{relative}/" or
                not isinstance(record, dict) or
                not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
            raise SystemExit(f"ERROR: unsafe wakeword feature file record: {relative!r}")
    manifest["wakeword"] = {
        "enabled": True,
        "activation": "automatic-after-microphone",
        "autostart": True,
        "engine": "openwakeword-onnx",
        "wake_word": "Alexa",
        "development_model": True,
        "model_license": "CC-BY-NC-SA-4.0",
        "threads": 2,
        "sample_rate_hz": 16000,
        "block_samples": 1280,
        "continuous_model_input": True,
        "vad": "native-energy-decision-gate",
        "aec": "speexdsp-fixed-10ms-200ms-tail",
        "microphone_calibration": "idme-q14",
        "external_payload": True,
        "payload": {
            "filename": payload.name,
            "sha256": payload_hash,
            "size": payload_size,
            "format": "squashfs-lz4",
            "manifest_sha256": sha256(read(payload_manifest)),
            "files": feature_files,
        },
    }


def read_external_feature(
        feature_id: str, payload: Path, payload_manifest: Path,
        required_files: tuple[str, ...]) -> tuple[str, int, dict[str, object]]:
    """Validate a generic external feature and return its recorded contents."""
    if payload.is_symlink() or not payload.is_file():
        raise SystemExit(
            f"ERROR: {feature_id} feature payload is not a regular file: {payload}"
        )
    if payload_manifest.is_symlink() or not payload_manifest.is_file():
        raise SystemExit(
            f"ERROR: {feature_id} feature manifest is not a regular file: "
            f"{payload_manifest}"
        )
    try:
        feature = json.loads(payload_manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"ERROR: {feature_id} feature manifest is invalid: {payload_manifest}"
        ) from exc
    if (not isinstance(feature, dict) or feature.get("schema_version") != 1 or
            feature.get("feature_id") != feature_id or
            feature.get("format") != "squashfs-lz4"):
        raise SystemExit(f"ERROR: {feature_id} feature manifest contract changed")
    feature_payload = feature.get("payload")
    feature_files = feature.get("files")
    if not isinstance(feature_payload, dict) or not isinstance(feature_files, dict):
        raise SystemExit(
            f"ERROR: {feature_id} feature manifest lacks payload/files records"
        )
    for required in required_files:
        if required not in feature_files:
            raise SystemExit(
                f"ERROR: {feature_id} feature member missing: {required}"
            )
    payload_hash = sha256(read(payload))
    payload_size = payload.stat().st_size
    if (feature_payload.get("filename") != payload.name or
            feature_payload.get("sha256") != payload_hash or
            feature_payload.get("size") != payload_size):
        raise SystemExit(
            f"ERROR: {feature_id} feature payload does not match its manifest"
        )
    for relative, record in feature_files.items():
        if (not isinstance(relative, str) or not relative or
                relative.startswith("/") or "//" in relative or
                "/../" in f"/{relative}/" or not isinstance(record, dict) or
                not re.fullmatch(r"[0-9a-f]{64}",
                                 str(record.get("sha256", "")))):
            raise SystemExit(
                f"ERROR: unsafe {feature_id} feature file record: {relative!r}"
            )
    return payload_hash, payload_size, feature_files


def add_stt_external_payload(payload: Path, payload_manifest: Path,
                             manifest: dict[str, object]) -> None:
    """Record the persistent English streaming STT runtime."""
    required_files = (
        "usr/local/sbin/libreecho-sttd",
        "usr/local/share/libreecho/stt/encoder-epoch-99-avg-1.int8.onnx",
        "usr/local/share/libreecho/stt/decoder-epoch-99-avg-1.int8.onnx",
        "usr/local/share/libreecho/stt/joiner-epoch-99-avg-1.int8.onnx",
        "usr/local/share/libreecho/stt/tokens.txt",
        "usr/local/share/licenses/libreecho-stt-model/MODEL-LICENSE.md",
        "usr/local/share/licenses/libreecho-stt-runtime/RUNTIME-NOTICES.txt",
        "usr/local/share/licenses/libreecho-stt-runtime/ONNX-Runtime-MIT.txt",
        "usr/local/share/licenses/libreecho-stt-runtime/sherpa-onnx-Apache-2.0.txt",
        "usr/local/share/licenses/libreecho-stt-runtime/SpeexDSP-COPYING.txt",
    )
    payload_hash, payload_size, feature_files = read_external_feature(
        "stt", payload, payload_manifest, required_files
    )
    manifest["stt"] = {
        "enabled": True,
        "activation": "automatic-before-assistant",
        "autostart": True,
        "engine": "sherpa-onnx-streaming-zipformer",
        "language": "en",
        "quantization": "int8",
        "threads": 2,
        "sample_rate_hz": 16000,
        "endpoint_trailing_silence_ms": 500,
        "streaming": True,
        "model_license": "Apache-2.0",
        "external_payload": True,
        "payload": {
            "filename": payload.name,
            "sha256": payload_hash,
            "size": payload_size,
            "format": "squashfs-lz4",
            "manifest_sha256": sha256(read(payload_manifest)),
            "files": feature_files,
        },
    }


def add_assistant_external_payload(payload: Path, payload_manifest: Path,
                                   manifest: dict[str, object]) -> None:
    """Record the provider-neutral streamed voice-assistant runtime."""
    required_files = (
        "usr/local/sbin/libreecho-agentd",
        "usr/local/libexec/libreecho-curl",
        "usr/local/share/libreecho/cacert.pem",
        "usr/local/share/licenses/curl/COPYING",
        "usr/local/share/licenses/ca-certificates/copyright",
        "usr/local/share/licenses/libreecho-assistant/THIRD_PARTY_NOTICES.txt",
        "usr/local/share/licenses/libreecho-assistant/OpenSSL-copyright",
        "usr/local/share/licenses/libreecho-assistant/glibc-copyright",
        "usr/local/share/licenses/libreecho-assistant/gcc-runtime-copyright",
    )
    payload_hash, payload_size, feature_files = read_external_feature(
        "assistant", payload, payload_manifest, required_files
    )
    manifest["assistant"] = {
        "enabled": True,
        "activation": "automatic-after-wake-stt-and-tts",
        "autostart": True,
        "provider": "openai-codex",
        "provider_neutral_boundary": True,
        "subscription_device_auth": True,
        "metered_api_key_auth": False,
        "text_streaming": True,
        "sentence_streaming_to_tts": True,
        "default_model": "gpt-5.4",
        "latency_target_ms": 3000,
        "credential_storage": "private-persistent-0600",
        "external_payload": True,
        "payload": {
            "filename": payload.name,
            "sha256": payload_hash,
            "size": payload_size,
            "format": "squashfs-lz4",
            "manifest_sha256": sha256(read(payload_manifest)),
            "files": feature_files,
        },
    }


def read_ssh_password_hash(path: Path) -> str:
    """Read one build-local crypt(3) hash without accepting a plaintext secret."""
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"ERROR: SSH password hash is not a regular file: {path}")
    if path.stat().st_mode & 0o022:
        raise SystemExit(f"ERROR: SSH password hash is group/world-writable: {path}")
    data = read(path)
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data or b"\n" in data or b"\r" in data:
        raise SystemExit("ERROR: SSH password hash must be exactly one line")
    try:
        value = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SystemExit("ERROR: SSH password hash is not ASCII") from exc
    if not SSH_PASSWORD_HASH_RE.fullmatch(value):
        raise SystemExit("ERROR: SSH password hash is not a supported salted crypt(3) hash")
    return value


def add_ssh_bundle(stage: Path, dropbear: Path, dropbearkey: Path,
                   password_hash: Path, manifest: dict[str, object]) -> None:
    """Install the opt-in password-only root SSH bundle."""
    hash_value = read_ssh_password_hash(password_hash)
    files: dict[str, object] = {}

    (stage / "root").mkdir(parents=True, exist_ok=True)
    (stage / "root").chmod(0o755)
    (stage / "etc/dropbear").mkdir(parents=True, exist_ok=True)
    (stage / "etc/dropbear").chmod(0o700)

    account_files = {
        "etc/passwd": (b"root:x:0:0:root:/root:/bin/sh\n", 0o644),
        "etc/group": (b"root:x:0:\n", 0o644),
        "etc/shells": (b"/bin/sh\n", 0o644),
        "etc/shadow": (f"root:{hash_value}:0:0:99999:7:::\n".encode("ascii"), 0o600),
    }
    for relative, (data, mode) in account_files.items():
        target = stage / relative
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: SSH account file collides with {target}")
        target.write_bytes(data)
        target.chmod(mode)
        record: dict[str, object] = {
            "path": "/" + relative,
            "size": len(data),
            "mode": f"{mode:04o}",
        }
        if relative == "etc/shadow":
            record["secret_content_not_recorded"] = True
        else:
            record["sha256"] = sha256(data)
        files[relative] = record

    for relative, source in (
        ("sbin/dropbear", dropbear),
        ("sbin/dropbearkey", dropbearkey),
    ):
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f"ERROR: SSH binary is not a regular file: {source}")
        data = read(source)
        if b"authorized_keys" in data:
            raise SystemExit(f"ERROR: public-key authorization marker found in {source}")
        target = stage / relative
        if target.exists() or target.is_symlink():
            raise SystemExit(f"ERROR: SSH binary collides with {target}")
        target.write_bytes(data)
        target.chmod(0o755)
        files[relative] = {
            "path": str(source.resolve()),
            "sha256": sha256(data),
            "size": len(data),
            "mode": "0755",
            "elf": require_elf_contract(target, 0x05000400, None, (), False),
        }

    manifest["ssh"] = {
        "enabled": True,
        "activation": "manual-only",
        "autostart": False,
        "authentication": "password-only",
        "public_key_auth": False,
        "root_login": True,
        "host_keys": "generated-ephemerally-under-/tmp/dropbear",
        "files": files,
    }


def add_network_bundle(stage: Path, wpa_supplicant: Path, wpa_metadata_path: Path,
                       wifi_config: Path,
                       manifest: dict[str, object]) -> None:
    """Add the verified static WPA client and a build-local Wi-Fi profile."""
    if wpa_supplicant.is_symlink() or not wpa_supplicant.is_file():
        raise SystemExit(f"ERROR: wpa_supplicant is not a regular file: {wpa_supplicant}")
    if wpa_metadata_path.is_symlink() or not wpa_metadata_path.is_file():
        raise SystemExit(f"ERROR: wpa source metadata is not a regular file: {wpa_metadata_path}")
    if wifi_config.is_symlink() or not wifi_config.is_file():
        raise SystemExit(f"ERROR: Wi-Fi profile is not a regular file: {wifi_config}")
    wpa_data = read(wpa_supplicant)
    try:
        wpa_metadata = json.loads(wpa_metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid wpa source metadata: {exc}") from exc
    required_metadata = {
        "binary_sha256", "binary_size", "build_epoch", "compiler", "config_path",
        "config_sha256", "crypto", "drivers", "kernel_uapi_sha256", "license",
        "source_sha256", "source_url", "static", "version",
    }
    if not isinstance(wpa_metadata, dict) or set(wpa_metadata) != required_metadata:
        raise SystemExit("ERROR: wpa source metadata schema mismatch")
    if (wpa_metadata["binary_sha256"] != sha256(wpa_data) or
            wpa_metadata["binary_size"] != len(wpa_data)):
        raise SystemExit("ERROR: wpa source metadata binary identity mismatch")
    if (wpa_metadata["source_sha256"] != WPA_SOURCE_SHA256 or
            wpa_metadata["source_url"] != WPA_SOURCE_URL or
            wpa_metadata["license"] != "BSD-3-Clause" or
            wpa_metadata["version"] != WPA_SUPPLICANT_VERSION or
            wpa_metadata["static"] is not True or
            not isinstance(wpa_metadata["kernel_uapi_sha256"], str) or
            not re.fullmatch(r"[0-9a-f]{64}", wpa_metadata["kernel_uapi_sha256"])):
        raise SystemExit("ERROR: wpa source metadata provenance is invalid")
    config_data = read(wifi_config)
    target = stage / "sbin/wpa_supplicant"
    if target.exists() or target.is_symlink():
        raise SystemExit(f"ERROR: network asset collides with {target}")
    target.write_bytes(wpa_data)
    target.chmod(0o755)
    elf = require_elf_contract(target, 0x05000400, None, (), False)
    if b"wpa_supplicant v2.10" not in wpa_data:
        raise SystemExit("ERROR: static wpa_supplicant does not identify as v2.10")
    if b"CHANGE_ME" in config_data:
        raise SystemExit("ERROR: refusing to package the unconfigured Wi-Fi profile template")
    config_target = stage / "etc/wifi/wpa_supplicant.conf"
    config_target.write_bytes(config_data)
    config_target.chmod(0o600)
    manifest["network"] = {
        "enabled": True,
        "activation": "manual-single-shot-after-adb",
        "wpa_supplicant": {
            "version": WPA_SUPPLICANT_VERSION,
            "sha256": sha256(wpa_data),
            "size": len(wpa_data),
            "mode": "0755",
            "elf": elf,
            "source": wpa_metadata,
        },
        "wifi_profile": {
            "sha256": sha256(config_data),
            "size": len(config_data),
            "mode": "0600",
            "secret_content_not_recorded": True,
        },
        "dhcp": "busybox-udhcpc",
        "dhcp_hook": "/etc/udhcpc.script",
    }


def validate_stage(stage: Path) -> None:
    required = (
        "init", "init.rc", "libreecho-init", "bin/busybox",
        "lib/ld-musl-armhf.so.1", "lib/libc.musl-armv7.so.1",
        "sbin/adbd", "sbin/ueventd", "sbin/sh", "system/bin/sh",
        "usr/local/sbin/libreecho-update", "usr/local/sbin/libreecho-bootctl",
        "usr/local/sbin/libreecho-data-cleanup",
        "usr/local/sbin/libreecho-update-fetch",
        "usr/local/libexec/libreecho-update-verify",
        "etc/libreecho/ota-public-key.hex", "etc/libreecho/ota-source.conf",
        "etc/libreecho/image-profile", "etc/libreecho/service-profile",
        "etc/libreecho/feature-policy",
        "etc/libreecho/first-install-confirm",
        "usr/local/share/licenses/libreecho-core/THIRD_PARTY_NOTICES.md",
        "usr/local/share/licenses/libreecho-core/COMPONENTS.json",
        "usr/local/share/licenses/libreecho-core/GPL-2.0-only.txt",
        "usr/local/share/licenses/libreecho-core/wpa_supplicant-BSD.txt",

    )
    for relative in required:
        if not (stage / relative).exists():
            raise SystemExit(f"ERROR: initramfs is missing {relative}")

    stage_root = stage.resolve()
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            components = target.split("/")
            if (
                not target
                or target.startswith("/")
                or "\0" in target
                or any(component in ("", ".") for component in components)
            ):
                raise SystemExit(f"ERROR: unsafe initramfs symlink: {path} -> {target!r}")
            try:
                path.resolve(strict=True).relative_to(stage_root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SystemExit(f"ERROR: initramfs symlink escapes, dangles, or loops: {path}") from exc
            continue
        if not path.is_file():
            continue
        ident = elf_identity(path)
        if ident is not None and ident != (1, 40):
            raise SystemExit(f"ERROR: non-ARM32 ELF in initramfs: {path} class={ident[0]} machine={ident[1]}")

    # /init is intentionally a script.  Only the native helper is required
    # to be a static ARM32 ELF here; treating the script as an ELF was the
    # stale-builder bug that allowed the stock PID 1 back into the image.
    output = subprocess.run(
        ["readelf", "-l", str(stage / "sbin/adbd")], check=True,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    if "Requesting program interpreter" in output:
        raise SystemExit("ERROR: sbin/adbd is not static")
    init_script = read(stage / "init")
    if init_script != read(stage / "libreecho-init"):
        raise SystemExit("ERROR: runtime /init differs from audited libreecho-init")
    if not init_script.startswith(b"#!/bin/busybox sh\n"):
        raise SystemExit("ERROR: runtime /init is not the audited BusyBox shell script")

    busybox_program = subprocess.run(
        ["readelf", "-l", str(stage / "bin/busybox")], check=True,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    busybox_dynamic = subprocess.run(
        ["readelf", "-d", str(stage / "bin/busybox")], check=True,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    if "/lib/ld-musl-armhf.so.1" not in busybox_program:
        raise SystemExit("ERROR: BusyBox interpreter contract changed")
    if "libc.musl-armv7.so.1" not in busybox_dynamic:
        raise SystemExit("ERROR: BusyBox DT_NEEDED contract changed")

    init_script = read(stage / "libreecho-init")
    for marker in (
        b"FASTBOOT_PLEASE", b"/tmp/runme", b"functionfs", b"/dev/stpwmt", b"/dev/stpbt",
        b"PARTNAME=expdb", b"/sys/class/block/mmcblk0p7", b"20480", b"bs=15 count=1",
        b"stat -c '%t:%T'",
    ):
        if marker not in init_script:
            raise SystemExit(f"ERROR: recovery control script lacks {marker!r}")
    adbd_launches = tuple(
        line.strip() for line in init_script.splitlines()
        if line.lstrip().startswith(b"/sbin/adbd ")
    )
    if adbd_launches != (
        b"/sbin/adbd --device_banner=device </dev/null >/tmp/adbd.log 2>&1 &",
    ):
        raise SystemExit(f"ERROR: unexpected ARM32 adbd launch contract: {adbd_launches!r}")
    for forbidden in (b"/proc/hps/enabled", b"scaling_governor", b"cpuidle"):
        if forbidden in init_script:
            raise SystemExit(f"ERROR: recovery control script contains forbidden policy override {forbidden!r}")
    properties = read(stage / "default.prop")
    for setting in (b"ro.boot.selinux=permissive", b"ro.secure=0", b"ro.debuggable=1", b"ro.adb.secure=0"):
        if setting not in properties.splitlines():
            raise SystemExit(f"ERROR: recovery property contract lacks {setting!r}")

    active_controls = sorted(
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*.rc")
        if path.is_file()
    )
    active_controls.append("libreecho-init")
    forbidden_launches = (
        b"wmt_loader", b"wmt_launcher", b"wmt_configure", b"wmt_responder", b"wmt_bt_on",
    )
    forbidden_wifi_writes = (
        b"> /dev/wmtWifi", b">/dev/wmtWifi", b"tee /dev/wmtWifi", b"of=/dev/wmtWifi",
    )
    for relative in active_controls:
        control = read(stage / relative)
        forbidden = () if relative == "libreecho-init" else forbidden_launches + forbidden_wifi_writes
        for marker in forbidden:
            if marker in control:
                raise SystemExit(f"ERROR: active recovery control {relative} contains {marker!r}")
        for line in control.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[:2] == [b"write", b"/dev/wmtWifi"]:
                raise SystemExit(
                    f"ERROR: active recovery control {relative} activates Wi-Fi through Android init"
                )
    if (stage / "init.connectivity.rc").exists():
        raise SystemExit("ERROR: auto-starting init.connectivity.rc is forbidden")


def build_cpio(stage: Path, epoch: int) -> bytes:
    for path in [stage, *sorted(stage.rglob("*"))]:
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    paths = sorted(
        (path.relative_to(stage) for path in stage.rglob("*")),
        key=lambda path: (len(path.parts), path.as_posix()),
    )
    names = b"".join(("./" + path.as_posix()).encode() + b"\0" for path in paths)
    result = subprocess.run(
        ["cpio", "--null", "--create", "--format=newc", "--owner=0:0", "--reproducible", "--quiet"],
        cwd=stage, input=names, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"}, check=True,
    )
    if not result.stdout.startswith(b"070701"):
        raise SystemExit("ERROR: generated initramfs is not a newc archive")
    return result.stdout


def extract_or_read_dtb(source: bytes, supplied: Path | None, expected: str | None) -> tuple[bytes, str]:
    del source
    if supplied is None:
        raise SystemExit("ERROR: generated boot envelope requires an explicit --dtb")
    raw = read(supplied)
    if expected is None:
        raise SystemExit("ERROR: --expected-dtb-sha256 is required with --dtb")
    require_hash("supplied DTB", raw, expected)
    origin = str(supplied.resolve())
    if raw[:4] != FDT_MAGIC or len(raw) < 8:
        raise SystemExit("ERROR: DTB magic missing")
    total = struct.unpack_from(">I", raw, 4)[0]
    if total > len(raw) or total > EVT_PADDED_SIZE:
        raise SystemExit(f"ERROR: invalid DTB totalsize {total:#x} for file size {len(raw):#x}")
    return raw[:total], origin


def padded_dtb(raw: bytes) -> bytes:
    result = bytearray(EVT_PADDED_SIZE)
    result[:len(raw)] = raw
    struct.pack_into(">I", result, 4, EVT_PADDED_SIZE)
    return bytes(result)


def system_map_end(path: Path, kernel_addr: int) -> tuple[int, dict[str, str]]:
    symbols: dict[str, int] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3:
            try:
                symbols.setdefault(fields[2], int(fields[0], 16))
            except ValueError:
                pass
    if "_text" not in symbols or "_end" not in symbols:
        raise SystemExit("ERROR: System.map lacks _text or _end")
    physical_end = kernel_addr + symbols["_end"] - symbols["_text"]
    return physical_end, {
        "sha256": sha256(read(path)),
        "_text": f"0x{symbols['_text']:08x}",
        "_end": f"0x{symbols['_end']:08x}",
        "physical_end": f"0x{physical_end:08x}",
    }


def package_boot(envelope: bytes, zimage: bytes, ramdisk: bytes, raw_dtb: bytes,
                 ramdisk_addr: int, system_map: Path | None) -> tuple[bytes, dict[str, object]]:
    if envelope[:8] != ANDROID_MAGIC or len(envelope) != IMAGE_SIZE:
        raise SystemExit("ERROR: generated envelope is not an exact 16 MiB Android boot envelope")
    fields = list(struct.unpack_from("<10I", envelope, 8))
    old_kernel_size, kernel_addr = fields[0], fields[1]
    old_ramdisk_size, old_ramdisk_addr = fields[2], fields[3]
    second_size, _second_addr, tags_addr, page_size, dt_size, _unused = fields[4:]
    if (kernel_addr, tags_addr, page_size, dt_size) != (KERNEL_ADDR, TAGS_ADDR, PAGE_SIZE, 0):
        raise SystemExit("ERROR: source Android address/page contract changed")
    if old_kernel_size or old_ramdisk_size or second_size or dt_size:
        raise SystemExit("ERROR: generated envelope contains nonzero payload sections")
    if not envelope[64:576].startswith(b"bootopt=64S3,32N2,32N2"):
        raise SystemExit("ERROR: generated bootopt no longer selects the proven 32-bit path")

    if len(zimage) < 0x30 or struct.unpack_from("<I", zimage, 0x24)[0] != ZIMAGE_MAGIC:
        raise SystemExit("ERROR: ARM zImage magic missing")
    if struct.unpack_from("<II", zimage, 0x28) != (0, len(zimage)):
        raise SystemExit("ERROR: zImage start/end fields do not match its file size")

    dtb = padded_dtb(raw_dtb)
    payload = zimage + dtb
    mkimg = bytearray(MKIMG_SIZE)
    mkimg[:4] = MKIMG_MAGIC
    mkimg[8:14] = b"KERNEL"
    struct.pack_into("<I", mkimg, 4, len(payload))
    kernel = bytes(mkimg) + payload

    kernel_file_end = kernel_addr + len(payload)
    if kernel_file_end >= ramdisk_addr:
        raise SystemExit("ERROR: loaded zImage/DTB payload overlaps the ramdisk")
    kernel_runtime_end = None
    system_map_record = None
    if system_map is not None:
        kernel_runtime_end, system_map_record = system_map_end(system_map, kernel_addr)
        if kernel_runtime_end > ATF_START:
            raise SystemExit("ERROR: decompressed kernel reaches the ATF reservation")
        if kernel_runtime_end >= ramdisk_addr:
            raise SystemExit("ERROR: decompressed kernel/BSS overlaps the ramdisk")
    ramdisk_end = ramdisk_addr + len(ramdisk)
    if ramdisk_addr < ATF_END or ramdisk_end > RAMDISK_END_LIMIT:
        raise SystemExit(
            f"ERROR: ramdisk physical range {ramdisk_addr:#x}-{ramdisk_end:#x} is outside "
            f"{ATF_END:#x}-{RAMDISK_END_LIMIT:#x}"
        )

    header = bytearray(envelope[:PAGE_SIZE])
    struct.pack_into("<I", header, 8, len(kernel))
    struct.pack_into("<I", header, 16, len(ramdisk))
    struct.pack_into("<I", header, 20, ramdisk_addr)
    second = b""
    outer_dt = b""
    header[576:608] = android_id(kernel, ramdisk, second, outer_dt)

    result = bytearray(header)
    result += kernel
    result += b"\0" * (align(len(result)) - len(result))
    ramdisk_file_offset = len(result)
    result += ramdisk
    result += b"\0" * (align(len(result)) - len(result))
    result += second
    result += b"\0" * (align(len(result)) - len(result))
    result += outer_dt
    if len(result) > IMAGE_SIZE:
        raise SystemExit(f"ERROR: image exceeds the 16 MiB boot envelope by {len(result) - IMAGE_SIZE:#x} bytes")
    result += b"\0" * (IMAGE_SIZE - len(result))

    record: dict[str, object] = {
        "android": {
            "image_size": len(result),
            "page_size": PAGE_SIZE,
            "kernel_size": len(kernel),
            "kernel_addr": f"0x{kernel_addr:08x}",
            "ramdisk_size": len(ramdisk),
            "ramdisk_addr": f"0x{ramdisk_addr:08x}",
            "ramdisk_file_offset": f"0x{ramdisk_file_offset:x}",
            "tags_addr": f"0x{tags_addr:08x}",
            "id": bytes(header[576:608]).hex(),
            "source_second_addr_preserved": f"0x{fields[5]:08x}",
        },
        "memory": {
            "loaded_payload": [f"0x{kernel_addr:08x}", f"0x{kernel_file_end:08x}"],
            "decompressed_kernel_end": None if kernel_runtime_end is None else f"0x{kernel_runtime_end:08x}",
            "atf": [f"0x{ATF_START:08x}", f"0x{ATF_END:08x}"],
            "ramdisk": [f"0x{ramdisk_addr:08x}", f"0x{ramdisk_end:08x}"],
            "ramdisk_end_limit": f"0x{RAMDISK_END_LIMIT:08x}",
            "ram_console_start": "0x44400000",
        },
        "mtk": {
            "header_sha256": sha256(bytes(mkimg)),
            "payload_size": len(payload),
            "zimage_size": len(zimage),
            "padded_dtb_size": len(dtb),
            "padded_dtb_sha256": sha256(dtb),
        },
        "system_map": system_map_record,
    }
    return bytes(result), record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--boot-envelope", type=Path, required=True)
    parser.add_argument("--adbd", type=Path, required=True,
                        help="source-built static ARM32 adbd")
    parser.add_argument("--adbd-source-metadata", type=Path, required=True,
                        help="source/license metadata emitted by build_adbd.sh")
    parser.add_argument("--busybox", type=Path, required=True)
    parser.add_argument("--expected-busybox-sha256", required=True)
    parser.add_argument("--musl-loader", type=Path, required=True)
    parser.add_argument("--expected-musl-loader-sha256", required=True)
    parser.add_argument("--image-profile", choices=("development", "ota"), required=True)
    parser.add_argument("--service-profile", choices=("diagnostic", "production"),
                        default="diagnostic")
    parser.add_argument(
        "--feature-policy",
        choices=("exclude", "preserve", "redistributable", "community-noncommercial"),
                        default="preserve")
    parser.add_argument("--update-channel", choices=("dev", "stable"),
                        required=True)
    parser.add_argument("--bootctl", type=Path, required=True)
    parser.add_argument("--update-verifier", type=Path, required=True)
    parser.add_argument("--ota-public-key", type=Path, required=True)
    parser.add_argument("--audio-probe", type=Path,
                        help="static ARM32 ALSA capability probe to add to the initramfs")
    parser.add_argument("--tinyplay", type=Path,
                        help="static ARM32 TinyALSA playback utility to add to the initramfs")
    parser.add_argument("--tinycap", type=Path,
                        help="static ARM32 TinyALSA capture utility to add to the initramfs")
    parser.add_argument("--tinymix", type=Path,
                        help="static ARM32 TinyALSA mixer utility to add to the initramfs")
    parser.add_argument("--iwconfig", type=Path,
                        help="static ARM32 wireless-tools iwconfig utility")
    parser.add_argument("--iwconfig-source-metadata", type=Path,
                        help="source/license metadata emitted by build_wireless_tools.sh")
    parser.add_argument("--ui-bundle", type=Path,
                        help="staged static ARM32 LibreEcho-UI bundle")
    parser.add_argument("--ui-source", type=Path,
                        help="LibreEcho-UI source checkout used for the bundle")
    parser.add_argument("--expected-ui-commit",
                        help="expected LibreEcho-UI source commit")
    parser.add_argument("--expected-ui-diff-sha256",
                        help="expected LibreEcho-UI source diff identity")
    parser.add_argument("--nqptp", type=Path,
                        help="static ARM32 NQPTP AirPlay 2 timing daemon")
    parser.add_argument("--shairport-sync", type=Path,
                        help="ARM32 AirPlay 2 Shairport Sync receiver")
    parser.add_argument("--avahi-daemon", type=Path,
                        help="ARM32 Avahi mDNS daemon for AirPlay discovery")
    parser.add_argument("--dbus-daemon", type=Path,
                        help="ARM32 D-Bus system daemon for Avahi")
    parser.add_argument("--airplay-runtime", type=Path,
                        help="ARMHF glibc runtime closure for Shairport Sync")
    parser.add_argument("--airplay-payload", type=Path,
                        help="external SquashFS AirPlay 2 feature payload")
    parser.add_argument("--airplay-payload-manifest", type=Path,
                        help="manifest for the external AirPlay 2 feature payload")
    parser.add_argument("--tts-payload", type=Path,
                        help="external SquashFS two-voice TTS feature payload")
    parser.add_argument("--tts-payload-manifest", type=Path,
                        help="manifest for the external two-voice TTS feature payload")
    parser.add_argument("--wakeword-payload", type=Path,
                        help="external SquashFS openWakeWord feature payload")
    parser.add_argument("--wakeword-payload-manifest", type=Path,
                        help="manifest for the external openWakeWord feature payload")
    parser.add_argument("--stt-payload", type=Path,
                        help="external SquashFS English streaming STT feature payload")
    parser.add_argument("--stt-payload-manifest", type=Path,
                        help="manifest for the external English STT feature payload")
    parser.add_argument("--assistant-payload", type=Path,
                        help="external SquashFS streamed assistant feature payload")
    parser.add_argument("--assistant-payload-manifest", type=Path,
                        help="manifest for the external assistant feature payload")

    parser.add_argument("--ssh-enabled", action="store_true",
                        help="explicitly enable the password-only root SSH bundle")
    parser.add_argument("--dropbear", type=Path,
                        help="static ARM32 password-only Dropbear server")
    parser.add_argument("--dropbearkey", type=Path,
                        help="static ARM32 Dropbear host-key utility")
    parser.add_argument("--ssh-root-password-hash", type=Path,
                        help="build-local salted root crypt(3) hash file")

    parser.add_argument("--wmt-config-helper", type=Path,
                        help="reviewed static ARM32 configure-only WMT helper")
    parser.add_argument("--wmt-responder", type=Path,
                        help="reviewed static ARM32 Gate2 WMT responder")
    parser.add_argument("--wmt-bt-on", type=Path,
                        help="reviewed static ARM32 one-shot BT-only helper")
    parser.add_argument("--wmt-stock-compat", type=Path,
                        help="proven ARM32 stock-compatible configure-only helper")
    parser.add_argument("--wmt-launcher", type=Path,
                        help="proven ARM32 one-shot WMT command responder")
    parser.add_argument("--wpa-supplicant", type=Path,
                        help="static ARM32 wpa_supplicant 2.10 client")
    parser.add_argument("--wpa-source-metadata", type=Path,
                        help="source/license metadata emitted by build_wpa_supplicant.sh")
    parser.add_argument("--wifi-config", type=Path,
                        help="build-local WPA profile; never committed to source")
    parser.add_argument("--qemu-arm", default="qemu-arm-static",
                        help="user-mode ARM emulator used to inventory pinned BusyBox applets")
    parser.add_argument("--zimage", type=Path, required=True)
    parser.add_argument("--expected-zimage-sha256", default=PROVEN_ZIMAGE_SHA256)
    parser.add_argument("--system-map", type=Path, required=True)
    parser.add_argument("--expected-system-map-sha256", default=PROVEN_SYSTEM_MAP_SHA256)
    parser.add_argument("--dtb", type=Path,
                        help="supplied pinned EVT DTB; omit only for the stock-DTB ADB parity stage")
    parser.add_argument("--expected-dtb-sha256")
    parser.add_argument("--ramdisk-address", type=parse_int, default=RAMDISK_ADDR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ramdisk-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    connectivity_options = {
        "wmt_config_helper": args.wmt_config_helper,
        "wmt_responder": args.wmt_responder,
        "wmt_bt_on": args.wmt_bt_on,
        "wmt_stock_compat": args.wmt_stock_compat,
        "wmt_launcher": args.wmt_launcher,
    }
    connectivity_enabled = all(value is not None for value in connectivity_options.values())
    if any(value is not None for value in connectivity_options.values()) and not connectivity_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in connectivity_options.items() if value is None
        )
        raise SystemExit(f"ERROR: connectivity bundle is all-or-nothing; missing {missing}")
    if connectivity_enabled and not CONNECTIVITY_HELPERS:
        raise SystemExit("ERROR: connectivity helper identities have not been pinned")
    network_options = {
        "wpa_supplicant": args.wpa_supplicant,
        "wpa_source_metadata": args.wpa_source_metadata,
        "wifi_config": args.wifi_config,
    }
    network_enabled = all(value is not None for value in network_options.values())
    if any(value is not None for value in network_options.values()) and not network_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in network_options.items() if value is None
        )
        raise SystemExit(f"ERROR: network stack is all-or-nothing; missing {missing}")
    audio_tool_options = {
        "tinyplay": args.tinyplay,
        "tinycap": args.tinycap,
        "tinymix": args.tinymix,
    }
    audio_tools_enabled = all(value is not None for value in audio_tool_options.values())
    if any(value is not None for value in audio_tool_options.values()) and not audio_tools_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in audio_tool_options.items() if value is None
        )
        raise SystemExit(f"ERROR: audio tools are all-or-nothing; missing {missing}")
    if audio_tools_enabled and args.audio_probe is None:
        raise SystemExit("ERROR: audio tools require --audio-probe")

    ssh_options = {
        "dropbear": args.dropbear,
        "dropbearkey": args.dropbearkey,
        "ssh_root_password_hash": args.ssh_root_password_hash,
    }
    ssh_enabled = args.ssh_enabled
    if ssh_enabled and not all(value is not None for value in ssh_options.values()):
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in ssh_options.items() if value is None
        )
        raise SystemExit(f"ERROR: SSH bundle is explicitly enabled but missing {missing}")
    if not ssh_enabled and any(value is not None for value in ssh_options.values()):
        raise SystemExit("ERROR: SSH inputs require the explicit --ssh-enabled opt-in")

    ui_options = {
        "ui_bundle": args.ui_bundle,
        "ui_source": args.ui_source,
        "expected_ui_commit": args.expected_ui_commit,
        "expected_ui_diff_sha256": args.expected_ui_diff_sha256,
    }
    ui_enabled = args.ui_bundle is not None
    if ui_enabled and not all(value is not None for value in ui_options.values()):
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in ui_options.items() if value is None
        )
        raise SystemExit(f"ERROR: UI bundle is enabled but missing {missing}")
    if not ui_enabled and any(value is not None for value in ui_options.values()):
        raise SystemExit("ERROR: UI inputs require the explicit --ui-bundle opt-in")

    airplay_legacy_options = {
        "nqptp": args.nqptp,
        "shairport_sync": args.shairport_sync,
        "avahi_daemon": args.avahi_daemon,
        "dbus_daemon": args.dbus_daemon,
        "airplay_runtime": args.airplay_runtime,
    }
    airplay_payload_options = {
        "airplay_payload": args.airplay_payload,
        "airplay_payload_manifest": args.airplay_payload_manifest,
    }
    airplay_legacy_enabled = all(value is not None for value in airplay_legacy_options.values())
    airplay_payload_enabled = all(value is not None for value in airplay_payload_options.values())
    if any(value is not None for value in airplay_legacy_options.values()) and not airplay_legacy_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in airplay_legacy_options.items() if value is None
        )
        raise SystemExit(f"ERROR: AirPlay inputs are all-or-nothing; missing {missing}")
    if any(value is not None for value in airplay_payload_options.values()) and not airplay_payload_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in airplay_payload_options.items() if value is None
        )
        raise SystemExit(f"ERROR: AirPlay payload inputs are all-or-nothing; missing {missing}")
    if airplay_legacy_enabled and airplay_payload_enabled:
        raise SystemExit("ERROR: choose embedded AirPlay assets or an external feature payload, not both")
    tts_payload_options = {
        "tts_payload": args.tts_payload,
        "tts_payload_manifest": args.tts_payload_manifest,
    }
    tts_payload_enabled = all(value is not None for value in tts_payload_options.values())
    if any(value is not None for value in tts_payload_options.values()) and not tts_payload_enabled:
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in tts_payload_options.items() if value is None
        )
        raise SystemExit(f"ERROR: TTS payload inputs are all-or-nothing; missing {missing}")
    wakeword_payload_options = {
        "wakeword_payload": args.wakeword_payload,
        "wakeword_payload_manifest": args.wakeword_payload_manifest,
    }
    wakeword_payload_enabled = all(
        value is not None for value in wakeword_payload_options.values()
    )
    if (any(value is not None for value in wakeword_payload_options.values()) and
            not wakeword_payload_enabled):
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in wakeword_payload_options.items() if value is None
        )
        raise SystemExit(
            f"ERROR: wakeword payload inputs are all-or-nothing; missing {missing}"
        )
    stt_payload_options = {
        "stt_payload": args.stt_payload,
        "stt_payload_manifest": args.stt_payload_manifest,
    }
    stt_payload_enabled = all(
        value is not None for value in stt_payload_options.values()
    )
    if (any(value is not None for value in stt_payload_options.values()) and
            not stt_payload_enabled):
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in stt_payload_options.items() if value is None
        )
        raise SystemExit(
            f"ERROR: STT payload inputs are all-or-nothing; missing {missing}"
        )
    assistant_payload_options = {
        "assistant_payload": args.assistant_payload,
        "assistant_payload_manifest": args.assistant_payload_manifest,
    }
    assistant_payload_enabled = all(
        value is not None for value in assistant_payload_options.values()
    )
    if (any(value is not None for value in assistant_payload_options.values()) and
            not assistant_payload_enabled):
        missing = ", ".join(
            "--" + name.replace("_", "-")
            for name, value in assistant_payload_options.items() if value is None
        )
        raise SystemExit(
            "ERROR: assistant payload inputs are all-or-nothing; "
            f"missing {missing}"
        )

    if args.feature_policy == "exclude":
        if args.service_profile != "diagnostic":
            raise SystemExit("ERROR: feature exclusion requires the diagnostic service profile")
        if any((
            airplay_legacy_enabled, airplay_payload_enabled, tts_payload_enabled,
            wakeword_payload_enabled, stt_payload_enabled, assistant_payload_enabled,
        )):
            raise SystemExit("ERROR: feature payload inputs are forbidden by feature_policy=exclude")
    elif args.feature_policy == "redistributable":
        if args.service_profile != "production":
            raise SystemExit("ERROR: redistributable policy requires the production service profile")
        if not all((airplay_payload_enabled, tts_payload_enabled,
                    stt_payload_enabled, assistant_payload_enabled)):
            raise SystemExit(
                "ERROR: redistributable policy requires external AirPlay, TTS, STT, and assistant payloads"
            )
        if airplay_legacy_enabled:
            raise SystemExit("ERROR: embedded AirPlay assets are forbidden by feature_policy=redistributable")
        if wakeword_payload_enabled:
            raise SystemExit(
                "ERROR: wakeword payload inputs are forbidden by feature_policy=redistributable"
            )
    elif args.feature_policy == "community-noncommercial":
        if args.service_profile != "production":
            raise SystemExit(
                "ERROR: community-noncommercial policy requires the production service profile"
            )
        if not all((airplay_payload_enabled, tts_payload_enabled,
                    wakeword_payload_enabled, stt_payload_enabled,
                    assistant_payload_enabled)):
            raise SystemExit("ERROR: community-noncommercial policy requires external AirPlay, TTS, wakeword, STT, and assistant payloads")
        if airplay_legacy_enabled:
            raise SystemExit(
                "ERROR: embedded AirPlay assets are forbidden by "
                "feature_policy=community-noncommercial"
            )

    envelope = read(args.boot_envelope)
    canonical_envelope = generate_boot_envelope()
    if envelope != canonical_envelope:
        raise SystemExit("ERROR: supplied boot envelope is not the canonical generated envelope")
    if sha256(envelope) != BOOT_ENVELOPE_SHA256:
        raise SystemExit("ERROR: canonical boot envelope digest changed")
    zimage = read(args.zimage)
    require_hash("ARM32 zImage", zimage, args.expected_zimage_sha256)
    system_map = read(args.system_map)
    require_hash("ARM32 System.map", system_map, args.expected_system_map_sha256)
    raw_dtb, dtb_origin = extract_or_read_dtb(envelope, args.dtb, args.expected_dtb_sha256)
    qemu_arm = shutil.which(args.qemu_arm)
    if qemu_arm is None:
        raise SystemExit(f"ERROR: ARM user-mode emulator not found: {args.qemu_arm}")

    output = args.output.resolve()
    ramdisk_output = (args.ramdisk_output or output.with_suffix(".ramdisk.cpio.gz")).resolve()
    manifest_output = (args.manifest or output.with_suffix(".manifest.json")).resolve()
    for path in (output, ramdisk_output, manifest_output):
        if path.exists():
            raise SystemExit(f"ERROR: refusing to overwrite {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "schema_version": 2,
        "name": "libreecho-mt8163-arm32-v97-recovery",
        "status": "PREPARED_NOT_FLASHED",
        "inputs": {
            "boot_envelope": {"path": str(args.boot_envelope.resolve()), "sha256": sha256(envelope)},
            "zimage": {"path": str(args.zimage.resolve()), "sha256": args.expected_zimage_sha256},
            "system_map": {
                "path": str(args.system_map.resolve()),
                "sha256": args.expected_system_map_sha256,
            },
            "dtb_origin": dtb_origin,
            "dtb_raw_sha256": sha256(raw_dtb),
            "dtb_raw_size": len(raw_dtb),
        },
        "connectivity": {
            "id": CONNECTIVITY_BUNDLE_ID,
            "enabled": False,
            "activation": "manual-gates-only",
            "autostart": False,
            "files": {},
            "helpers": {},
            "symlinks": {},
        },
        "network": {
            "enabled": False,
            "activation": "passive-until-profile-is-supplied",
        },
        "audio": {
            "enabled": False,
            "activation": "manual-only",
            "probe": {},
            "tools": {},
        },
        "network_tools": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
            "tools": {},
        },
        "ssh": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
            "authentication": "password-only",
            "public_key_auth": False,
            "root_login": True,
            "host_keys": "generated-ephemerally-under-/tmp/dropbear",
            "files": {},
        },
        "ui": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
            "hardware_ownership": "existing-control-plane",
            "files": {},
        },
        "airplay": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
            "protocol": "airplay2",
            "audio_transport": "shairport-pipe-to-shared-priority-engine",
            "tinyalsa_pcm": "hw:0,23",
            "runtime": {},
        },
        "tts": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
        },
        "wakeword": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
        },
        "stt": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
        },
        "assistant": {
            "enabled": False,
            "activation": "manual-only",
            "autostart": False,
        },
    }
    overlay = Path(__file__).resolve().parent / "initramfs"
    with tempfile.TemporaryDirectory(prefix="libreecho-arm32-initramfs-") as temporary:
        stage = Path(temporary)
        copy_adbd(args.adbd.resolve(), args.adbd_source_metadata.resolve(), stage, manifest)
        add_overlay(
            stage, overlay, args.busybox.resolve(), args.musl_loader.resolve(),
            args.expected_busybox_sha256, args.expected_musl_loader_sha256,
            qemu_arm, manifest,
        )
        add_ota_tools(
            stage, args.bootctl.resolve(), args.update_verifier.resolve(),
            args.ota_public_key.resolve(), args.image_profile,
            args.service_profile, args.feature_policy, args.update_channel, manifest,
        )
        if args.audio_probe is not None:
            add_audio_probe(stage, args.audio_probe.resolve(), manifest)
        if audio_tools_enabled:
            add_audio_tools(
                stage, args.tinyplay.resolve(), args.tinycap.resolve(),
                args.tinymix.resolve(), manifest,
            )
        if args.iwconfig is not None:
            if args.iwconfig_source_metadata is None:
                raise SystemExit("ERROR: iwconfig source metadata is required")
            add_network_tools(
                stage, args.iwconfig.resolve(),
                args.iwconfig_source_metadata.resolve(), manifest,
            )
        if ui_enabled:
            add_ui_bundle(
                stage, args.ui_bundle.resolve(), args.ui_source.resolve(),
                args.expected_ui_commit, args.expected_ui_diff_sha256, manifest,
            )
        if airplay_payload_enabled:
            add_airplay_external_payload(
                args.airplay_payload.resolve(), args.airplay_payload_manifest.resolve(), manifest,
            )
        elif airplay_legacy_enabled:
            add_airplay_bundle(
                stage, args.nqptp.resolve(), args.shairport_sync.resolve(),
                args.avahi_daemon.resolve(), args.dbus_daemon.resolve(),
                args.airplay_runtime.resolve(), manifest,
            )
        if tts_payload_enabled:
            add_tts_external_payload(
                args.tts_payload.resolve(), args.tts_payload_manifest.resolve(), manifest,
            )
        if wakeword_payload_enabled:
            add_wakeword_external_payload(
                args.wakeword_payload.resolve(),
                args.wakeword_payload_manifest.resolve(),
                manifest,
            )
        if stt_payload_enabled:
            add_stt_external_payload(
                args.stt_payload.resolve(),
                args.stt_payload_manifest.resolve(),
                manifest,
            )
        if assistant_payload_enabled:
            add_assistant_external_payload(
                args.assistant_payload.resolve(),
                args.assistant_payload_manifest.resolve(),
                manifest,
            )

        if ssh_enabled:
            add_ssh_bundle(
                stage, args.dropbear.resolve(), args.dropbearkey.resolve(),
                args.ssh_root_password_hash.resolve(), manifest,
            )
        if connectivity_enabled:
            add_connectivity_bundle(
                stage, {
                    "wmt_config_helper": args.wmt_config_helper.absolute(),
                    "wmt_responder": args.wmt_responder.absolute(),
                    "wmt_bt_on": args.wmt_bt_on.absolute(),
                    "wmt_stock_compat": args.wmt_stock_compat.absolute(),
                    "wmt_launcher": args.wmt_launcher.absolute(),
                },
                manifest,
            )
        if network_enabled:
            add_network_bundle(
                stage, args.wpa_supplicant.resolve(), args.wpa_source_metadata.resolve(),
                args.wifi_config.resolve(), manifest,
            )
        validate_stage(stage)
        cpio = build_cpio(stage, 0)
    ramdisk = gzip.compress(cpio, compresslevel=9, mtime=0)
    if ramdisk[:4] != b"\x1f\x8b\x08\x00" or gzip.decompress(ramdisk) != cpio:
        raise SystemExit("ERROR: deterministic gzip round trip failed")

    boot, package_record = package_boot(
        envelope, zimage, ramdisk, raw_dtb, args.ramdisk_address,
        args.system_map.resolve(),
    )
    ramdisk_output.write_bytes(ramdisk)
    output.write_bytes(boot)
    manifest["initramfs"] = {
        "cpio_sha256": sha256(cpio),
        "cpio_size": len(cpio),
        "gzip_sha256": sha256(ramdisk),
        "gzip_size": len(ramdisk),
        "path": str(ramdisk_output),
    }
    manifest["package"] = package_record
    manifest["output"] = {"path": str(output), "sha256": sha256(boot), "size": len(boot)}
    manifest_output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    print(f"boot_image={output}")
    print(f"boot_sha256={sha256(boot)}")
    print(f"ramdisk={ramdisk_output}")
    print(f"ramdisk_sha256={sha256(ramdisk)}")
    print(f"manifest={manifest_output}")
    print("status=PREPARED_NOT_FLASHED")


if __name__ == "__main__":
    main()
