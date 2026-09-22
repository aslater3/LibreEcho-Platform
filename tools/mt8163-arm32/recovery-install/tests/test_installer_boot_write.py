#!/usr/bin/env python3
"""Host-side test for the installer's boot-slot write.

The installer runs as one file under TWRP's mksh with ``set -u``. On the Dot
that combination turned a typo into a silent partial install: the loop's
per-slot message referenced an unset ``$slot`` instead of ``$write_slot``, mksh
aborted the function at that expansion, and the run wrote ``boot_a`` only --
``boot_b`` kept the previous image and neither slot was verified -- while the
receipt still said ``installed``.

The function is extracted from the shipped ``update-binary`` and run under a
real shell against scratch slot files, so both the write and the readback
verification are exercised as the device runs them.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import hashlib

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent / "src/META-INF/com/google/android/update-binary"
BOOT_BYTES = 32768 * 512  # one 16 MiB slot


def extract_function(source: str, name: str) -> str:
    """Return one shell function's text, whichever brace style it uses."""
    for marker in (f"{name}()\n{{\n", f"{name}() {{"):
        if marker in source:
            break
    else:
        raise AssertionError(f"function not found: {name}")
    start = source.index(marker)
    depth = 0
    for offset, char in enumerate(source[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"unterminated function: {name}")


class BootSlotWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="le-bootwrite-"))
        self.bundle = self.work / "bundle"
        self.bundle.mkdir()
        self.image = self.bundle / "libreecho-test-boot.img"
        self.image.write_bytes(b"ANDROID!" + bytes(BOOT_BYTES - 8))
        self.slot_a = self.work / "boot_a"
        self.slot_b = self.work / "boot_b"
        # Both slots start as something else, so a skipped write is visible.
        self.slot_a.write_bytes(b"previous-a" * (BOOT_BYTES // 10))
        self.slot_b.write_bytes(b"previous-b" * (BOOT_BYTES // 10))
        self.log = self.work / "install.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def run_write(self, *, corrupt_b: bool = False) -> subprocess.CompletedProcess[str]:
        function = extract_function(INSTALLER.read_text(), "write_boot_slots")
        harness = self.work / "harness.sh"
        harness.write_text(
            "#!/bin/sh\n"
            "set -u\n"
            f'BUNDLE_DIR="{self.bundle}"\n'
            "BOOT_SLOT_SECTORS=32768\n"
            "DRY_RUN=0\n"
            f'LOG="{self.log}"\n'
            'ui_print() { printf "%s\\n" "$*" >> "$LOG"; }\n'
            'log_line() { printf "%s\\n" "$*" >> "$LOG"; }\n'
            'die() { printf "FAILED: %s\\n" "$*" >> "$LOG"; exit 1; }\n'
            'sha256_of() { /usr/bin/sha256sum "$1" 2>/dev/null | cut -d" " -f1; }\n'
            'partition_node() { case "$1" in boot_a) printf "%s\\n" "$SLOT_A" ;; '
            'boot_b) printf "%s\\n" "$SLOT_B" ;; *) return 1 ;; esac; }\n'
            f'SLOT_A="{self.slot_a}"\n'
            f'SLOT_B="{self.slot_b}"\n'
            f"{function}\n"
            'write_boot_slots "$(basename "$IMAGE")"\n'
            'printf "rc=%s\\n" "$?"\n')
        env = os.environ.copy()
        env["IMAGE"] = str(self.image)
        os.chmod(harness, os.stat(harness).st_mode | stat.S_IEXEC)
        return subprocess.run(["sh", str(harness)], text=True, capture_output=True, env=env)

    def logged(self) -> str:
        return self.log.read_text() if self.log.is_file() else ""

    def test_both_slots_are_written_and_verified(self) -> None:
        result = self.run_write()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        wanted = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.assertEqual(hashlib.sha256(self.slot_a.read_bytes()).hexdigest(), wanted)
        self.assertEqual(hashlib.sha256(self.slot_b.read_bytes()).hexdigest(), wanted)
        # One line per slot, named: a run that silently skipped a slot used to
        # still report success, so the messages are part of the evidence.
        self.assertIn("boot_a verified", self.logged())
        self.assertIn("boot_b verified", self.logged())

    def test_the_function_does_not_reference_an_undefined_slot_variable(self) -> None:
        """The exact defect: `$slot`, never set, under TWRP's `set -u`."""
        body = extract_function(INSTALLER.read_text(), "write_boot_slots")
        self.assertIn("$write_slot verified", body)
        self.assertNotIn('"  $slot ', body)

    def test_a_slot_that_does_not_read_back_is_a_failure(self) -> None:
        # Point both slot names at files and then make the readback impossible by
        # asking for a slot whose node cannot be hashed after the write: use a
        # target that is not the image by writing to it between dd and the check.
        function = extract_function(INSTALLER.read_text(), "write_boot_slots")
        harness = self.work / "mismatch.sh"
        harness.write_text(
            "#!/bin/sh\n"
            "set -u\n"
            f'BUNDLE_DIR="{self.bundle}"\n'
            "BOOT_SLOT_SECTORS=32768\n"
            "DRY_RUN=0\n"
            f'LOG="{self.log}"\n'
            'ui_print() { printf "%s\\n" "$*" >> "$LOG"; }\n'
            'die() { printf "FAILED: %s\\n" "$*" >> "$LOG"; exit 1; }\n'
            'sha256_of() { case "$1" in *boot.img) /usr/bin/sha256sum "$1" 2>/dev/null | cut -d" " -f1 ;; '
            '*) printf "%s\\n" "0000000000000000000000000000000000000000000000000000000000000000" ;; esac; }\n'
            'partition_node() { printf "%s\\n" "$SLOT_A"; }\n'
            f'SLOT_A="{self.slot_a}"\n'
            f"{function}\n"
            'write_boot_slots "$(basename "$IMAGE")"\n'
            'printf "rc=%s\\n" "$?"\n')
        env = os.environ.copy()
        env["IMAGE"] = str(self.image)
        result = subprocess.run(["sh", str(harness)], text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readback mismatch", self.logged())


if __name__ == "__main__":
    unittest.main(verbosity=2)
