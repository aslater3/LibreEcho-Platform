#!/usr/bin/env python3
"""Byte-exact offline tests for the stock MT8163 ROM-patch routing ABI."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PATCHES = (
    "ROMv2_lm_patch_1_0_hdr.bin",
    "ROMv2_lm_patch_1_1_hdr.bin",
)


class PatchRoutingTests(unittest.TestCase):
    binary: Path
    firmware: Path
    qemu: str

    def run_inspection(self, firmware: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                self.qemu,
                str(self.binary),
                "--inspect-patches",
                "--firmware-dir",
                str(firmware),
            ),
            check=False,
            capture_output=True,
            text=True,
        )

    def staged_firmware(self, root: Path) -> Path:
        for name in PATCHES:
            shutil.copyfile(self.firmware / name, root / name)
        return root

    def test_stock_route_decoding(self) -> None:
        result = self.run_inspection(self.firmware)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "seq=2 size=128720 header=8a:00 route=22:00:06:00 "
            "address=00:00:06:00",
            result.stdout,
        )
        self.assertIn(
            "seq=1 size=50148 header=8a:00 route=21:00:0e:f0 "
            "address=00:00:0e:f0",
            result.stdout,
        )

    def test_owner_system_patch_sizes_keep_stock_routes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wmt-owner-patch-test-") as temporary:
            staged = self.staged_firmware(Path(temporary))
            expected = {
                PATCHES[0]: 127596,
                PATCHES[1]: 50952,
            }
            for name, size in expected.items():
                patch = staged / name
                data = patch.read_bytes()
                patch.write_bytes((data + bytes(size))[:size])
            result = self.run_inspection(staged)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "seq=2 size=127596 header=8a:00 route=22:00:06:00 "
                "address=00:00:06:00",
                result.stdout,
            )
            self.assertIn(
                "seq=1 size=50952 header=8a:00 route=21:00:0e:f0 "
                "address=00:00:0e:f0",
                result.stdout,
            )

    def test_shifted_or_filename_order_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wmt-route-test-") as temporary:
            staged = self.staged_firmware(Path(temporary))
            patch = staged / PATCHES[0]
            data = bytearray(patch.read_bytes())
            data[24] = 0x21
            patch.write_bytes(data)
            result = self.run_inspection(staged)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected patch route", result.stderr)

    def test_two_byte_header_is_pinned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wmt-header-test-") as temporary:
            staged = self.staged_firmware(Path(temporary))
            patch = staged / PATCHES[1]
            data = bytearray(patch.read_bytes())
            data[23] = 0x01
            patch.write_bytes(data)
            result = self.run_inspection(staged)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected patch header", result.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--qemu", required=True)
    args = parser.parse_args()
    PatchRoutingTests.binary = args.binary.resolve()
    PatchRoutingTests.firmware = args.firmware.resolve()
    PatchRoutingTests.qemu = args.qemu
    unittest.main(argv=[__file__])


if __name__ == "__main__":
    main()
