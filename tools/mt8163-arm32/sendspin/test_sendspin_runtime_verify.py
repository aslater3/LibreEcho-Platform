#!/usr/bin/env python3
"""Unit tests for the reviewed ARMHF runtime closure verifier.

Dependency-free and hermetic: package locks, fake archives and synthetic
``readelf -V`` text are generated in temp dirs, so these run in the default suite
without a cross toolchain or the reviewed debs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_sendspin_runtime as r  # noqa: E402

NEEDS_TEXT = """\
Version needs section '.gnu.version_r' contains 2 entries:
 Addr: 0x000002b0  Offset: 0x0002b0  Link: 3 (.dynstr)
  000000: Version: 1  File: libc.so.6  Cnt: 3
  0x0010:   Name: GLIBC_2.4  Flags: none  Version: 2
  0x0020:   Name: GLIBC_2.34  Flags: none  Version: 3
  0x0030:   Name: GLIBC_2.38  Flags: none  Version: 4
  0x0040: Version: 1  File: libm.so.6  Cnt: 1
  0x0050:   Name: GLIBC_2.29  Flags: none  Version: 2
"""

DEFINED_TEXT = """\
Version definition section '.gnu.version_d' contains 4 entries:
 Addr: 0x000002a8  Offset: 0x0002a8  Link: 3 (.dynstr)
  000000: Rev: 1  Flags: BASE  Index: 1  Cnt: 1  Name: libc.so.6
  0x001c: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: GLIBC_2.4
  0x0038: Rev: 1  Flags: none  Index: 3  Cnt: 1  Name: GLIBC_2.34
  0x0054: Rev: 1  Flags: none  Index: 4  Cnt: 1  Name: GLIBC_2.38
"""


class RuntimeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- package lock + staging --------------------------------------------
    def _write_lock(self, records) -> Path:
        path = self.root / "mdns-packages.lock.json"
        path.write_text(json.dumps({"packages": records}), encoding="utf-8")
        return path

    def test_load_package_lock_rejects_incomplete_record(self) -> None:
        path = self.root / "bad.json"
        path.write_text(json.dumps({"packages": [{"package": "libc6"}]}), encoding="utf-8")
        with self.assertRaises(r.RuntimeVerificationError):
            r.load_package_lock(path)

    def test_verify_and_stage_rejects_deb_hash_mismatch(self) -> None:
        archives = self.root / "archives"
        archives.mkdir()
        (archives / "libc6_2.39_armhf.deb").write_bytes(b"not the real deb")
        lock = r.load_package_lock(self._write_lock([
            {"package": "libc6", "file": "libc6_2.39_armhf.deb", "sha256": "0" * 64,
             "version": "2.39", "architecture": "armhf"},
        ]))
        with self.assertRaises(r.RuntimeVerificationError):
            r.verify_and_stage(lock, archives, self.root / "stage", ["libc6"])

    def test_verify_and_stage_rejects_missing_archive(self) -> None:
        archives = self.root / "archives"
        archives.mkdir()
        lock = r.load_package_lock(self._write_lock([
            {"package": "libc6", "file": "libc6_2.39_armhf.deb", "sha256": "0" * 64,
             "version": "2.39", "architecture": "armhf"},
        ]))
        with self.assertRaises(r.RuntimeVerificationError):
            r.verify_and_stage(lock, archives, self.root / "stage", ["libc6"])

    def test_verify_and_stage_rejects_unpinned_package(self) -> None:
        archives = self.root / "archives"
        archives.mkdir()
        lock = r.load_package_lock(self._write_lock([
            {"package": "libc6", "file": "libc6_2.39_armhf.deb", "sha256": "0" * 64,
             "version": "2.39", "architecture": "armhf"},
        ]))
        with self.assertRaises(r.RuntimeVerificationError):
            r.verify_and_stage(lock, archives, self.root / "stage", ["libstdc++6"])

    # --- glibc review bound -------------------------------------------------
    def _fake_root(self, version: bytes) -> Path:
        root = self.root / f"root{version.decode()}"
        (root / "usr/lib").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        (root / "usr/lib/libc.so.6").write_bytes(b"ELF...release version " + version + b"...")
        (root / "lib/ld-linux-armhf.so.3").write_bytes(b"loader")
        return root

    def test_assert_reviewed_glibc_accepts_2_39(self) -> None:
        self.assertIn("glibc 2.39", r.assert_reviewed_glibc(self._fake_root(b"2.39")))

    def test_assert_reviewed_glibc_rejects_newer_ad_hoc(self) -> None:
        with self.assertRaises(r.RuntimeVerificationError):
            r.assert_reviewed_glibc(self._fake_root(b"2.43"))

    # --- readelf version parsers -------------------------------------------
    def test_required_versions_parser(self) -> None:
        required = r.required_versions(NEEDS_TEXT)
        self.assertEqual(required["libc.so.6"], {"GLIBC_2.4", "GLIBC_2.34", "GLIBC_2.38"})
        self.assertEqual(required["libm.so.6"], {"GLIBC_2.29"})

    def test_required_versions_ignores_definition_section(self) -> None:
        combined = NEEDS_TEXT + DEFINED_TEXT
        self.assertNotIn("__def__", r.required_versions(combined))
        self.assertEqual(r.required_versions(combined), r.required_versions(NEEDS_TEXT))

    def test_defined_versions_parser(self) -> None:
        defined = r.defined_versions(DEFINED_TEXT)
        self.assertEqual(defined, {"libc.so.6", "GLIBC_2.4", "GLIBC_2.34", "GLIBC_2.38"})

    def test_symbol_closure_passes_when_covered(self) -> None:
        required = r.required_versions(NEEDS_TEXT)
        defined = {"libc.so.6": r.defined_versions(DEFINED_TEXT),
                   "libm.so.6": {"libm.so.6", "GLIBC_2.29"}}
        r.check_symbol_closure(required, defined)  # must not raise

    def test_symbol_closure_detects_missing_version(self) -> None:
        required = {"libc.so.6": {"GLIBC_2.4", "GLIBC_2.40"}}
        defined = {"libc.so.6": {"GLIBC_2.4", "GLIBC_2.38"}}
        with self.assertRaises(r.RuntimeVerificationError):
            r.check_symbol_closure(required, defined)

    def test_symbol_closure_detects_absent_library(self) -> None:
        with self.assertRaises(r.RuntimeVerificationError):
            r.check_symbol_closure({"libm.so.6": {"GLIBC_2.29"}}, {})


if __name__ == "__main__":
    unittest.main()
