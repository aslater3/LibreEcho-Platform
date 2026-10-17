#!/usr/bin/env python3
"""Host tests for libreecho-bootctl gpt-status / gpt-restore (boot-chain repair).

The disk images are synthetic: real Radar/Biscuit extents with generated GUIDs,
so no device identity is committed. Images are sparse files.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "ota/libreecho_bootctl.c"
SECTOR = 512
ENTRIES = 128
ENTRY_SIZE = 128
BASIC_DATA = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7").bytes_le

# Linux partition number -> (name, first LBA, last LBA). Legacy Amonet Radar,
# as left by the 0.13 BROM install (userdata shortened, wrappers appended).
RADAR_LEGACY = {
    1: ("kb", 0x800, 0xFFF), 2: ("dkb", 0x1000, 0x17FF),
    3: ("lk_a", 0x8000, 0x87FF), 4: ("tee1", 0xC000, 0xE7FF),
    5: ("lk_b", 0x10000, 0x107FF), 6: ("tee2", 0x14000, 0x167FF),
    7: ("expdb", 0x18000, 0x1CFFF), 8: ("misc", 0x1D000, 0x1D400),
    9: ("persist", 0x20000, 0x27FFF), 10: ("boot_a_x", 0x28000, 0x2FFFF),
    11: ("boot_b_x", 0x30000, 0x37FFF), 12: ("recovery", 0x38000, 0x3FFFF),
    13: ("system_a", 0x48000, 0x1C7FFF), 14: ("system_b", 0x1C8000, 0x347FFF),
    15: ("cache", 0x348000, 0x4CFFFF), 16: ("userdata", 0x4D0000, 0x6D9BFF),
    17: ("boot_a", 0x6D9C00, 0x710BFF), 18: ("boot_b", 0x710C00, 0x747BFF),
}
RADAR_DISK_SECTORS = 0x748000
BISCUIT_DISK_SECTORS = 0x74C000


def crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def entry(name: str, first: int, last: int) -> bytes:
    raw = BASIC_DATA + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
    return raw + name.encode("utf-16le").ljust(72, b"\0")


def header(current: int, alternate: int, entries_lba: int, last_usable: int,
           disk_guid: bytes, entries_crc: int) -> bytes:
    fields = struct.pack(
        "<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0, current, alternate,
        0x22, last_usable, disk_guid, entries_lba, ENTRIES, ENTRY_SIZE, entries_crc)
    sealed = fields[:16] + struct.pack("<I", crc(fields)) + fields[20:]
    return sealed.ljust(SECTOR, b"\0")


def build_disk(path: Path, layout: dict[int, tuple[str, int, int]], disk_sectors: int) -> bytes:
    table = bytearray(ENTRIES * ENTRY_SIZE)
    for number, (name, first, last) in layout.items():
        table[(number - 1) * ENTRY_SIZE:number * ENTRY_SIZE] = entry(name, first, last)
    table = bytes(table)
    last_lba = disk_sectors - 1
    guid = uuid.uuid4().bytes_le
    with path.open("wb") as disk:
        disk.truncate(disk_sectors * SECTOR)
        mbr = bytearray(SECTOR)
        mbr[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF]) + struct.pack("<II", 1, min(last_lba, 0xFFFFFFFF))
        mbr[510:512] = b"\x55\xaa"
        disk.write(mbr)
        disk.write(header(1, last_lba, 2, last_lba - 33, guid, crc(table)))
        disk.write(table)
        disk.seek((last_lba - 32) * SECTOR)
        disk.write(table)
        disk.write(header(last_lba, 1, last_lba - 32, last_lba - 33, guid, crc(table)))
    return table


def read_side(path: Path, header_lba: int) -> tuple[bytes, bytes]:
    with path.open("rb") as disk:
        disk.seek(header_lba * SECTOR)
        head = disk.read(SECTOR)
        entries_lba = struct.unpack_from("<Q", head, 72)[0]
        disk.seek(entries_lba * SECTOR)
        return head, disk.read(ENTRIES * ENTRY_SIZE)


def header_ok(head: bytes, table: bytes) -> bool:
    copy = head[:16] + b"\0\0\0\0" + head[20:92]
    return (struct.unpack_from("<I", head, 16)[0] == crc(copy)
            and struct.unpack_from("<I", head, 88)[0] == crc(table))


def names(table: bytes) -> dict[int, tuple[str, int, int]]:
    out = {}
    for index in range(ENTRIES):
        raw = table[index * ENTRY_SIZE:(index + 1) * ENTRY_SIZE]
        if raw[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", raw, 32)
        out[index + 1] = (raw[56:128].decode("utf-16le").rstrip("\0"), first, last)
    return out


@unittest.skipUnless(shutil.which("cc") or shutil.which("gcc"), "host C compiler required")
class BootChainGptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.work = Path(tempfile.mkdtemp(prefix="bootctl-gpt."))
        cls.binary = cls.work / "libreecho-bootctl"
        compiler = shutil.which("cc") or shutil.which("gcc")
        subprocess.run([compiler, "-std=c99", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra",
                        "-Werror", "-O2", "-DLIBREECHO_BOOTCTL_HOST_TEST", "-o", str(cls.binary),
                        str(SOURCE)], check=True, timeout=120)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.work, ignore_errors=True)

    def run_tool(self, disk: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, LIBREECHO_GPT_DEVICE=str(disk))
        return subprocess.run([str(self.binary), *args], env=env, text=True,
                              capture_output=True, timeout=60)

    def disk(self, layout=RADAR_LEGACY, sectors=RADAR_DISK_SECTORS) -> tuple[Path, bytes]:
        path = self.work / f"disk-{uuid.uuid4().hex}.img"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path, build_disk(path, layout, sectors)

    def test_status_reports_legacy_layout(self) -> None:
        path, _ = self.disk()
        result = self.run_tool(path, "gpt-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpt_layout=legacy-amonet\n", result.stdout)
        self.assertIn("gpt_userdata_sectors=2137088\n", result.stdout)

    def test_plan_does_not_write(self) -> None:
        path, _ = self.disk()
        before = path.read_bytes()[:64 * SECTOR]
        result = self.run_tool(path, "gpt-restore", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpt_restore=planned\n", result.stdout)
        self.assertIn("gpt_layout=stock\n", result.stdout)
        self.assertEqual(path.read_bytes()[:64 * SECTOR], before)

    def test_write_restores_pinned_layout_and_keeps_userdata(self) -> None:
        path, original = self.disk()
        mbr_before = path.read_bytes()[:SECTOR]
        result = self.run_tool(path, "gpt-restore", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        last = RADAR_DISK_SECTORS - 1
        primary_head, primary = read_side(path, 1)
        backup_head, backup = read_side(path, last)
        self.assertTrue(header_ok(primary_head, primary))
        self.assertTrue(header_ok(backup_head, backup))
        self.assertEqual(primary, backup)
        after = names(primary)
        expected = {n: v for n, v in RADAR_LEGACY.items() if n not in (17, 18)}
        expected[10] = ("boot_a", 0x28000, 0x2FFFF)
        expected[11] = ("boot_b", 0x30000, 0x37FFF)
        self.assertEqual(after, expected)
        # Unchanged entries keep every byte (type, unique GUID, attributes).
        for number in range(1, 17):
            if number in (10, 11):
                continue
            span = slice((number - 1) * ENTRY_SIZE, number * ENTRY_SIZE)
            self.assertEqual(primary[span], original[span], number)
        for number in (10, 11):
            span = slice((number - 1) * ENTRY_SIZE, (number - 1) * ENTRY_SIZE + 56)
            self.assertEqual(primary[span], original[span], number)
        self.assertEqual(path.read_bytes()[:SECTOR], mbr_before)
        status = self.run_tool(path, "gpt-status")
        self.assertIn("gpt_layout=stock\n", status.stdout)
        self.assertIn("gpt_userdata_sectors=2137088\n", status.stdout)

    def test_restore_is_refused_once_already_stock(self) -> None:
        path, _ = self.disk()
        self.assertEqual(self.run_tool(path, "gpt-restore", "--write").returncode, 0)
        again = self.run_tool(path, "gpt-restore", "--write")
        self.assertEqual(again.returncode, 1)
        self.assertIn("gpt_not_legacy_amonet", again.stderr)

    def test_biscuit_variant_userdata_restores(self) -> None:
        layout = dict(RADAR_LEGACY)
        layout[16] = ("userdata", 0x4D0000, 0x4D0000 + 2153472 - 1)
        layout[17] = ("boot_a", 0x6DDC00, 0x714BFF)
        layout[18] = ("boot_b", 0x714C00, 0x74BBFF)
        path, _ = self.disk(layout, BISCUIT_DISK_SECTORS)
        result = self.run_tool(path, "gpt-restore", "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpt_userdata_sectors=2153472\n", result.stdout)

    def test_refuses_unexpected_layouts(self) -> None:
        cases = {
            "wrapper-before-userdata": {**RADAR_LEGACY, 17: ("boot_a", 0x40000, 0x47FFF)},
            "store-wrong-size": {**RADAR_LEGACY, 10: ("boot_a_x", 0x28000, 0x2FFFE)},
            "duplicate-name": {**RADAR_LEGACY, 19: ("boot_a_x", 0x747C00, 0x747CFF)},
            "overlap": {**RADAR_LEGACY, 18: ("boot_b", 0x710000, 0x747BFF)},
            "missing-store": {n: v for n, v in RADAR_LEGACY.items() if n != 11},
        }
        for label, layout in cases.items():
            with self.subTest(label):
                path, _ = self.disk(layout)
                before = path.read_bytes()[:64 * SECTOR]
                result = self.run_tool(path, "gpt-restore", "--write")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(path.read_bytes()[:64 * SECTOR], before)

    def test_refuses_corrupt_or_inconsistent_tables(self) -> None:
        path, _ = self.disk()
        with path.open("r+b") as disk:  # corrupt the primary entry array
            disk.seek(2 * SECTOR + 5)
            disk.write(b"\xff")
        self.assertIn("gpt_unreadable", self.run_tool(path, "gpt-restore", "--write").stderr)
        path, table = self.disk()
        with path.open("r+b") as disk:  # backup header no longer at the last LBA
            disk.truncate((RADAR_DISK_SECTORS + 8) * SECTOR)
        self.assertIn("gpt_unreadable", self.run_tool(path, "gpt-status").stderr)

    def test_layout_commands_still_require_device_contract(self) -> None:
        # The BCB commands keep validating real /dev/mmcblk0p* nodes, never the test image.
        path, _ = self.disk()
        result = self.run_tool(path, "status")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
