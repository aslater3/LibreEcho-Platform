#!/usr/bin/env python3
"""Disk-backed initial-install transaction model for BISCUIT/MT8163.

This models the storage and transport boundary around BROM/Amonet. It does not
claim to emulate the MediaTek USB wire protocol or secure hardware.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

SECTOR = 512
DISK_SECTORS = 8_388_608  # 4 GiB sparse backing store
BOOT_BYTES = 16 * 1024 * 1024
ANDROID_MAGIC = b"ANDROID!"
BOOTOPT = b"bootopt=64S3,32N2,32N2"

# Physical GPT observed in the real Amonet/LK capture. Entries 17/18 are
# deliberately free before the wrapper is installed.
PHYSICAL = {
    1: (2048, 2048, "kb"),
    2: (4096, 2048, "dkb"),
    3: (32768, 2048, "lk_a"),
    4: (49152, 10240, "tee1"),
    5: (65536, 2048, "lk_b"),
    6: (81920, 10240, "tee2"),
    7: (98304, 20480, "expdb"),
    8: (118784, 1025, "misc"),
    9: (131072, 32768, "persist"),
    10: (163840, 32768, "boot_a"),
    11: (196608, 32768, "boot_b"),
    12: (229376, 32768, "recovery"),
    13: (294912, 1572864, "system_a"),
    14: (1867776, 1572864, "system_b"),
    15: (3440640, 1605632, "cache"),
    16: (5046272, 2605023, "userdata"),
}
WRAPPERS = {
    17: (0x6D9C00, 0x37000, "boot_a"),
    18: (0x710C00, 0x37000, "boot_b"),
}
VIRTUAL_NAMES = {10: "boot_a_x", 11: "boot_b_x"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _guid_bytes(value: uuid.UUID) -> bytes:
    return value.bytes_le


def write_gpt(image: Path, entries: dict[int, tuple[int, int, str]]) -> None:
    image.parent.mkdir(parents=True, exist_ok=True)
    with image.open("wb") as f:
        f.truncate(DISK_SECTORS * SECTOR)
    disk_guid = uuid.UUID("ed000d32-77b3-42df-a111-2ce3f85a2610")
    type_guid = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
    raw = bytearray(128 * 128)
    for number, (first, count, name) in entries.items():
        if first < 34 or first + count > DISK_SECTORS - 33:
            raise ValueError(f"partition {number} outside disk")
        off = (number - 1) * 128
        raw[off:off + 16] = _guid_bytes(type_guid)
        raw[off + 16:off + 32] = _guid_bytes(uuid.uuid5(disk_guid, name + str(number)))
        struct.pack_into("<QQ", raw, off + 32, first, first + count - 1)
        raw[off + 56:off + 128] = name.encode("utf-16le")[:72].ljust(72, b"\0")
    entries_crc = binascii.crc32(raw) & 0xffffffff
    primary = bytearray(SECTOR)
    primary[:8] = b"EFI PART"
    struct.pack_into("<I", primary, 8, 0x00010000)
    struct.pack_into("<I", primary, 12, 92)
    struct.pack_into("<Q", primary, 24, 1)
    struct.pack_into("<Q", primary, 32, DISK_SECTORS - 1)
    struct.pack_into("<Q", primary, 40, 34)
    struct.pack_into("<Q", primary, 48, DISK_SECTORS - 34)
    primary[56:72] = _guid_bytes(disk_guid)
    struct.pack_into("<Q", primary, 72, 2)
    struct.pack_into("<I", primary, 80, 128)
    struct.pack_into("<I", primary, 84, 128)
    struct.pack_into("<I", primary, 88, entries_crc)
    crc = binascii.crc32(primary[:92]) & 0xffffffff
    struct.pack_into("<I", primary, 16, crc)
    with image.open("r+b") as f:
        # Protective MBR for legacy partition scanners and QEMU's block parser.
        mbr = bytearray(SECTOR)
        mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0\0\0", 0xEE, b"\xff\xff\xff", 1, min(DISK_SECTORS - 1, 0xFFFFFFFF))
        mbr[510:512] = b"\x55\xaa"
        f.seek(0); f.write(mbr)
        f.seek(SECTOR); f.write(primary); f.write(raw)
        f.seek((DISK_SECTORS - 33) * SECTOR); f.write(raw)
        backup = bytearray(primary)
        struct.pack_into("<Q", backup, 24, DISK_SECTORS - 1)
        struct.pack_into("<Q", backup, 32, 1)
        struct.pack_into("<Q", backup, 72, DISK_SECTORS - 33)
        struct.pack_into("<I", backup, 16, 0)
        struct.pack_into("<I", backup, 16, binascii.crc32(backup[:92]) & 0xffffffff)
        f.seek((DISK_SECTORS - 1) * SECTOR); f.write(backup)


def read_gpt(image: Path) -> dict[int, tuple[int, int, str]]:
    with image.open("rb") as f:
        f.seek(SECTOR + 72)
        entry_lba = struct.unpack("<Q", f.read(8))[0]
        f.seek(entry_lba * SECTOR)
        raw = f.read(128 * 128)
    result = {}
    for number in range(1, 129):
        off = (number - 1) * 128
        if raw[off:off + 16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", raw, off + 32)
        name = raw[off + 56:off + 128].decode("utf-16le").split("\0", 1)[0]
        result[number] = (first, last - first + 1, name)
    return result


def write_partition(image: Path, entry: tuple[int, int, str], data: bytes) -> None:
    first, count, _ = entry
    capacity = count * SECTOR
    if len(data) > capacity:
        raise ValueError(f"payload {len(data)} exceeds partition capacity {capacity}")
    with image.open("r+b") as f:
        f.seek(first * SECTOR); f.write(data)
        if len(data) < capacity:
            f.write(b"\0" * min(capacity - len(data), 4096))


def make_boot(path: Path) -> None:
    data = bytearray(BOOT_BYTES)
    data[:8] = ANDROID_MAGIC
    struct.pack_into("<I", data, 8, 1)
    data[64:64 + len(BOOTOPT)] = BOOTOPT
    path.write_bytes(data)


def create_physical(image: Path) -> None:
    write_gpt(image, PHYSICAL)
    # Keep userdata a real filesystem so the QEMU guest can mount it after the
    # install transaction. The image remains sparse; only the filesystem
    # metadata is materialized.
    userdata = PHYSICAL[16]
    subprocess.run(
        ["mke2fs", "-F", "-q", "-t", "ext4", "-L", "userdata", "-E",
         f"offset={userdata[0] * SECTOR}", image, str(0x209C00 // 8 * 4)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    # Seed a valid-looking BCB and deterministic stock markers.
    gpt = read_gpt(image)
    with image.open("r+b") as f:
        f.seek(gpt[8][0] * SECTOR + 0x360)
        f.write(b"\0ABB\1\x8f\0")
        f.seek(gpt[10][0] * SECTOR); f.write(b"STOCK-BOOT-A\n")
        f.seek(gpt[11][0] * SECTOR); f.write(b"STOCK-BOOT-B\n")


def brom_install(image: Path, state_path: Path) -> None:
    gpt = read_gpt(image)
    for n, (first, count, name) in PHYSICAL.items():
        got = gpt.get(n)
        if got is None or got[:2] != (first, count) or got[2] != name:
            raise RuntimeError(f"physical GPT mismatch at p{n}: {got!r}")
    transformed = dict(PHYSICAL)
    transformed[10] = (*transformed[10][:2], "boot_a_x")
    transformed[11] = (*transformed[11][:2], "boot_b_x")
    # LK's post-wrapper view ends userdata at the observed boot_a wrapper
    # start (0x6d9c00), rather than exposing the complete physical extent.
    transformed[16] = (transformed[16][0], 0x209C00, "userdata")
    transformed.update(WRAPPERS)
    write_gpt(image, transformed)
    state = {
        "schema": "libreecho-initial-install-v1",
        "brom": "mocked-boundary",
        "product": "BISCUIT",
        "physical_gpt_validated": True,
        "wrapper_partitions": [17, 18],
        "redirects": {"boot_a": "boot_a_x", "boot_b": "boot_b_x"},
        "progress": ["Found port = /dev/mock-brom", "all good", "Check GPT", "Inject payload", "Force fastboot", "Reboot to unlocked fastboot"],
    }
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fastboot_flash(image: Path, boot: Path, slot: str, state_path: Path) -> None:
    if slot not in {"a", "b"}:
        raise ValueError(slot)
    gpt = read_gpt(image)
    expected = "boot_a_x" if slot == "a" else "boot_b_x"
    entry = next((e for e in gpt.values() if e[2] == expected), None)
    if entry is None or entry[1] != BOOT_BYTES // SECTOR:
        raise RuntimeError(f"missing reviewed payload partition {expected}")
    if boot.stat().st_size != BOOT_BYTES:
        raise RuntimeError("boot image must be exactly 16 MiB")
    if boot.read_bytes()[:8] != ANDROID_MAGIC or BOOTOPT not in boot.read_bytes()[:576]:
        raise RuntimeError("boot image contract invalid")
    write_partition(image, entry, boot.read_bytes())
    record = json.loads(state_path.read_text())
    record.setdefault("fastboot", []).append({"command": "flash", "partition": expected, "sha256": sha256(boot)})
    state_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def verify(image: Path, boot: Path, state_path: Path) -> dict[str, object]:
    gpt = read_gpt(image)
    required = {10: "boot_a_x", 11: "boot_b_x", 16: "userdata", 17: "boot_a", 18: "boot_b"}
    for n, name in required.items():
        if gpt.get(n, (None, None, None))[2] != name:
            raise AssertionError(f"logical GPT p{n} != {name}")
    expected = sha256(boot)
    hashes = {}
    with image.open("rb") as f:
        for n in (10, 11):
            first, count, _ = gpt[n]
            f.seek(first * SECTOR); hashes[gpt[n][2]] = hashlib.sha256(f.read(count * SECTOR)).hexdigest()
    if any(value != expected for value in hashes.values()):
        raise AssertionError(f"payload readback mismatch: {hashes}")
    state = json.loads(state_path.read_text())
    if state["redirects"] != {"boot_a": "boot_a_x", "boot_b": "boot_b_x"}:
        raise AssertionError("redirect contract missing")
    return {"gpt": "PASS", "brom": "PASS", "fastboot": "PASS", "payload_readback": "PASS", "boot_contract": "PASS", "sha256": expected}


def prepare_qemu_initramfs(base: Path, root: Path, expected: str) -> Path:
    initroot = root / "qemu-initramfs"
    initroot.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        f"gzip -dc {base} | cpio -idmu",
        shell=True,
        cwd=initroot,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (initroot / "expected").mkdir(exist_ok=True)
    (initroot / "expected" / "boot.sha256").write_text(expected + "\n")
    (initroot / "init").write_text(
        "#!/bin/busybox sh\n"
        "BB=/bin/busybox\n"
        "export PATH=/bin:/sbin:/usr/bin:/usr/sbin\n"
        "$BB mount -t proc proc /proc\n$BB mount -t sysfs sys /sys\n"
        "$BB mount -t devtmpfs dev /dev 2>/dev/null || $BB mdev -s\n"
        "if [ -f /mods/loadorder ]; then for m in $($BB cat /mods/loadorder); do $BB insmod /mods/$m 2>/dev/null || true; done; fi\n"
        "for m in /mods/*.ko; do [ -f \"$m\" ] && $BB insmod \"$m\" 2>/dev/null || true; done\n"
        "$BB sleep 2; $BB mdev -s 2>/dev/null || true\n"
        "i=0; while [ ! -e /sys/class/block/vda10/uevent ] || [ ! -b /dev/vda16 ]; do i=$((i+1)); [ $i -lt 30 ] || exit 10; $BB sleep 1; $BB mdev -s 2>/dev/null || true; done\n"
        "check() { set -- $($BB sha256sum \"$1\"); [ \"$1\" = \"$($BB cat /expected/boot.sha256)\" ]; }\n"
        "$BB grep -q '^PARTNAME=boot_a_x$' /sys/class/block/vda10/uevent || exit 11\n"
        "$BB grep -q '^PARTNAME=boot_b_x$' /sys/class/block/vda11/uevent || exit 12\n"
        "check /dev/vda10 || exit 13\ncheck /dev/vda11 || exit 14\n"
        "$BB mkdir -p /data\n"
        "$BB touch /data/.libreecho-qemu-boot-check; $BB sync\n"
        "echo QEMU_BOOT_CHECK=PASS\n$BB poweroff -f\n",
        encoding="ascii",
    )
    (initroot / "init").chmod(0o755)
    output = root / "qemu-initramfs.cpio.gz"
    subprocess.run(
        f"find . -print | cpio -o -H newc 2>/dev/null | gzip -c > {output}",
        shell=True,
        cwd=initroot,
        check=True,
    )
    return output


def run_qemu_boot(image: Path, base_initramfs: Path, kernel: Path, root: Path, expected: str) -> None:
    initramfs = prepare_qemu_initramfs(base_initramfs, root, expected)
    result = subprocess.run(
        ["qemu-system-arm", "-M", "virt", "-m", "512", "-cpu", "cortex-a15",
         "-kernel", str(kernel), "-initrd", str(initramfs),
         "-append", "console=ttyAMA0 rdinit=/init",
         "-drive", f"file={image},format=raw,if=none,id=hd0",
         "-device", "virtio-blk-device,drive=hd0", "-nographic", "-no-reboot"],
        text=True, capture_output=True, timeout=180,
    )
    if result.returncode != 0 or "QEMU_BOOT_CHECK=PASS" not in result.stdout:
        detail = (result.stdout + "\n" + result.stderr)[-4000:]
        raise RuntimeError(f"QEMU boot check failed: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--boot", type=Path)
    parser.add_argument("--qemu-kernel", type=Path)
    parser.add_argument("--qemu-initramfs", type=Path)
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    image = args.workdir / "emmc.img"
    state = args.workdir / "transaction.json"
    boot = args.boot or (args.workdir / "boot.img")
    if not args.boot: make_boot(boot)
    create_physical(image)
    brom_install(image, state)
    fastboot_flash(image, boot, "a", state)
    fastboot_flash(image, boot, "b", state)
    result = verify(image, boot, state)
    if args.qemu_kernel and args.qemu_initramfs:
        run_qemu_boot(image, args.qemu_initramfs, args.qemu_kernel, args.workdir, str(result["sha256"]))
        result["qemu_boot"] = "PASS"
    else:
        result["qemu_boot"] = "NOT_RUN (provide --qemu-kernel and --qemu-initramfs)"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
