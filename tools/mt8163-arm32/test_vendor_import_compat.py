#!/usr/bin/env python3
"""Regression tests for the 0.13.15 owner-local MT8163 importer contract."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent
IMPORTER = TOOLS_DIR / "initramfs/libreecho-vendor-import"
SHIPPED_SPEC_DIR = TOOLS_DIR / "initramfs/vendor-assets"
V2_SPEC = SHIPPED_SPEC_DIR / "mt8163-v181-stock-v2.tsv"
TARGETS = (
    "ROMv2_lm_patch_1_0_hdr.bin",
    "ROMv2_lm_patch_1_1_hdr.bin",
    "WIFI_RAM_CODE_8163",
    "WMT_SOC.cfg",
)


def patch_payload(meta: bytes, fill: int) -> bytes:
    payload = bytearray([fill] * 28)
    payload[22:28] = meta
    return bytes(payload)


def unknown_payloads() -> dict[str, bytes]:
    return {
        "ROMv2_lm_patch_1_0_hdr.bin": patch_payload(bytes.fromhex("8a0022000600"), 0x31),
        "ROMv2_lm_patch_1_1_hdr.bin": patch_payload(bytes.fromhex("8a0021000ef0"), 0x42),
        "WIFI_RAM_CODE_8163": b"unknown-owner-local-wifi-code",
        "WMT_SOC.cfg": b"unknown-owner-local-wmt-config\n",
    }


def write_payloads(root: Path, payloads: dict[str, bytes]) -> Path:
    layout = root / "system/vendor/firmware"
    layout.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (layout / name).write_bytes(payload)
    return layout


def manifest_lines(payloads: dict[str, bytes], source_layout: str = "system/vendor/firmware") -> str:
    lines = []
    for name in TARGETS:
        payload = payloads[name]
        lines.append(
            f"{hashlib.sha256(payload).hexdigest()}|{len(payload)}|"
            f"{source_layout}/{name}|{name}\n"
        )
    return "".join(lines)


class VendorImporterCompatTests(unittest.TestCase):
    def run_importer(
        self,
        root: Path,
        source: Path,
        *,
        spec_dir: Path = SHIPPED_SPEC_DIR,
        force: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        data = root / "data"
        config = data / "libreecho/config"
        firmware = root / "runtime-firmware"
        config.mkdir(parents=True, exist_ok=True)
        firmware.mkdir(parents=True, exist_ok=True)
        force_marker = config / "vendor-import-force-next-boot"
        if force:
            force_marker.write_text("force-unverified-owner-local-import-v1\n")
            force_marker.chmod(0o600)
        environment = {
            **os.environ,
            "LIBREECHO_VENDOR_TEST_MODE": "1",
            "DATA_ROOT": str(data),
            "LIBREECHO_VENDOR_SOURCE_ROOT": str(source),
            "LIBREECHO_VENDOR_FIRMWARE_ROOT": str(firmware),
            "LIBREECHO_VENDOR_SPEC_DIR": str(spec_dir),
            "LIBREECHO_VENDOR_STAGE_PARENT": str(root / "vendor-stage"),
            "LIBREECHO_VENDOR_STATUS_PATH": str(root / "run/libreecho/vendor-import.status"),
        }
        result = subprocess.run(
            ["/bin/sh", str(IMPORTER)],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result, config / "vendor-assets.tsv", Path(environment["LIBREECHO_VENDOR_STATUS_PATH"])

    def test_second_validated_revision_is_shipped_as_one_atomic_manifest(self) -> None:
        self.assertEqual(
            V2_SPEC.read_text(),
            "".join(
                (
                    "b4460117f51a43f3284594ec08d8c8861ecc0e42b17820987da03ecabdebac1e|128720|etc/firmware/ROMv2_lm_patch_1_0_hdr.bin|ROMv2_lm_patch_1_0_hdr.bin\n",
                    "10c4ed22a10b8a136bffd7ffce4d552300d76f8e593627d2a9841c3b11a5697e|50148|etc/firmware/ROMv2_lm_patch_1_1_hdr.bin|ROMv2_lm_patch_1_1_hdr.bin\n",
                    "9669cc9b03cfdc5e8fd4fd6e14c4c4050e8c196738ca4707eea12f14a6a8e64c|373840|etc/firmware/WIFI_RAM_CODE_8163|WIFI_RAM_CODE_8163\n",
                    "302bd4462de99c028c04092e561c1500d65582ce42a93c4c72ccae6e2c99013d|119|etc/firmware/WMT_SOC.cfg|WMT_SOC.cfg\n",
                )
            ),
        )

    def test_forced_unknown_set_enrols_hashes_then_verifies_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            payloads = unknown_payloads()
            write_payloads(source, payloads)

            first, enrolled, status = self.run_importer(root, source, force=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("verification=forced-unverified\n", status.read_text())
            self.assertTrue(enrolled.is_file())
            self.assertFalse(enrolled.is_symlink())
            self.assertEqual(stat.S_IMODE(enrolled.stat().st_mode), 0o600)
            self.assertEqual(enrolled.read_text(), manifest_lines(payloads))
            self.assertNotIn(b"unknown-owner-local-wifi-code", enrolled.read_bytes())

            second, enrolled_again, status_again = self.run_importer(root, source)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(enrolled_again.read_text(), manifest_lines(payloads))
            self.assertIn("verification=owner-local-enrolled\n", status_again.read_text())

    def test_changed_enrolled_set_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            payloads = unknown_payloads()
            layout = write_payloads(source, payloads)
            first, enrolled, _ = self.run_importer(root, source, force=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(enrolled.exists())

            (layout / "WIFI_RAM_CODE_8163").write_bytes(b"changed-after-enrolment")
            second, _, status = self.run_importer(root, source)
            self.assertEqual(second.returncode, 2)
            self.assertIn("VENDOR_IMPORT_ENROLLED_SET_MISMATCH", second.stderr)
            self.assertIn("error=VENDOR_IMPORT_ENROLLED_SET_MISMATCH\n", status.read_text())

    def test_rows_from_different_approved_revisions_are_never_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            spec_dir = root / "specs"
            spec_dir.mkdir()
            revision_one = {
                name: f"revision-one-{name}".encode() for name in TARGETS
            }
            revision_two = {
                name: f"revision-two-{name}".encode() for name in TARGETS
            }
            (spec_dir / "mt8163-v181-stock-v1.tsv").write_text(manifest_lines(revision_one))
            (spec_dir / "mt8163-v181-stock-v2.tsv").write_text(manifest_lines(revision_two))
            mixed = {
                TARGETS[0]: revision_one[TARGETS[0]],
                TARGETS[1]: revision_one[TARGETS[1]],
                TARGETS[2]: revision_two[TARGETS[2]],
                TARGETS[3]: revision_two[TARGETS[3]],
            }
            write_payloads(source, mixed)

            result, _, status = self.run_importer(root, source, spec_dir=spec_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("VENDOR_IMPORT_NO_HASH_PINNED_SET", result.stderr)
            self.assertIn("error=VENDOR_IMPORT_NO_HASH_PINNED_SET\n", status.read_text())

    def test_safe_structurally_compatible_unknown_set_is_offerable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            source.mkdir()
            outside = root / "outside-firmware"
            outside.mkdir()
            (source / "vendor").mkdir()
            (source / "vendor/firmware").symlink_to(outside, target_is_directory=True)
            write_payloads(source, unknown_payloads())

            result, _, status = self.run_importer(root, source)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("VENDOR_IMPORT_SOURCE_PATH_SYMLINK", result.stderr)
            self.assertIn("VENDOR_IMPORT_UNKNOWN_COMPATIBLE_SET", result.stderr)
            status_text = status.read_text()
            self.assertIn("error=VENDOR_IMPORT_UNKNOWN_COMPATIBLE_SET\n", status_text)
            self.assertIn("source_layout=system/vendor/firmware\n", status_text)

    def test_malformed_unknown_set_is_not_offerable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            payloads = unknown_payloads()
            payloads["ROMv2_lm_patch_1_0_hdr.bin"] = patch_payload(b"broken", 0x31)
            write_payloads(source, payloads)

            result, _, status = self.run_importer(root, source)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("VENDOR_IMPORT_UNKNOWN_COMPATIBLE_SET", result.stderr)
            self.assertIn("VENDOR_IMPORT_NO_HASH_PINNED_SET", result.stderr)
            self.assertIn("error=VENDOR_IMPORT_NO_HASH_PINNED_SET\n", status.read_text())


if __name__ == "__main__":
    unittest.main()
