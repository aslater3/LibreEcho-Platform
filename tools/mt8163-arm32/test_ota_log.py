#!/usr/bin/env python3
"""Persistent OTA event log (ota.log) written by the fetcher and installer."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
FETCH = TOOLS / "initramfs/libreecho-update-fetch"
UPDATE = TOOLS / "initramfs/libreecho-update"
CLEANUP = TOOLS / "initramfs/libreecho-data-cleanup"
BUSYBOX = "/bin/busybox"
LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (update|update-fetch)\[\d+\] \S.*$")


def ota_log_block(path: Path) -> str:
    text = path.read_text()
    start = text.index("# --- ota_log")
    end = text.index("# --- end ota_log ---", start)
    return text[start:end]


class OtaLogFunctionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ota-log-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.update = self.root / "update"
        self.update.mkdir()
        self.log = self.update / "ota.log"

    def run_log(self, *calls: str, max_bytes: int = 65536, log: Path | None = None):
        script = (f"BB={BUSYBOX}\nOTA_LOG={log or self.log}\n"
                  f"OTA_LOG_MAX_BYTES={max_bytes}\nOTA_LOG_SOURCE=update-fetch\n"
                  + ota_log_block(FETCH) + "\n" + "\n".join(calls) + "\necho rc=$?\n")
        return subprocess.run([BUSYBOX, "sh", "-c", script], capture_output=True,
                              text=True, timeout=30)

    def test_copies_in_fetcher_and_installer_are_identical(self) -> None:
        self.assertEqual(ota_log_block(FETCH), ota_log_block(UPDATE))

    def test_line_format_and_no_stdout(self) -> None:
        # The fetcher logs inside $(download_and_inspect), whose stdout is the
        # package version, so ota_log must print nothing.
        result = self.run_log('ota_log "start action=check channel=stable"')
        self.assertEqual(result.stdout, "rc=0\n")
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], LINE)
        self.assertTrue(lines[0].endswith("start action=check channel=stable"))
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_one_line_per_event_and_length_cap(self) -> None:
        self.run_log("ota_log \"error detail=$(printf 'a\\r\\nb')\"",
                     f'ota_log "long {"x" * 2000}"')
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("error detail=a  b"))
        self.assertLessEqual(len(lines[1]), 512)

    def test_rotates_to_two_files_at_most(self) -> None:
        calls = [f'ota_log "event number={i:04d} {"y" * 80}"' for i in range(40)]
        result = self.run_log(*calls, max_bytes=1024)
        self.assertTrue(result.stdout.endswith("rc=0\n"))
        names = sorted(p.name for p in self.update.iterdir())
        self.assertEqual(names, ["ota.log", "ota.log.1"])
        self.assertLessEqual(self.log.stat().st_size, 1024 + 512)
        self.assertIn("number=0039", self.log.read_text())

    def test_never_writes_through_a_symlink(self) -> None:
        target = self.root / "elsewhere"
        target.write_text("untouched\n")
        self.log.symlink_to(target)
        result = self.run_log('ota_log "event"')
        self.assertEqual(result.stdout, "rc=0\n")
        self.assertEqual(target.read_text(), "untouched\n")

    def test_missing_directory_is_not_created_and_never_fails(self) -> None:
        missing = self.root / "absent/ota.log"
        result = self.run_log('ota_log "event"', log=missing)
        self.assertEqual(result.stdout, "rc=0\n")
        self.assertFalse(missing.parent.exists())


class FetcherDieLogTests(unittest.TestCase):
    def test_die_records_reason_and_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-log-die-") as directory:
            root = Path(directory)
            update = root / "update"
            update.mkdir()
            source = FETCH.read_text()
            source = source[:source.rfind('case "${1:-}" in')]
            source = source.replace("ROOT=/data/libreecho/update", f"ROOT={update}")
            source += ('\ndie download_http true 22 '
                       '"HTTP 503 from https://dl.libreecho.org/x" 503\n')
            result = subprocess.run([BUSYBOX, "sh", "-c", source], capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 1)
            line = (update / "ota.log").read_text().splitlines()[-1]
            self.assertRegex(line, LINE)
            self.assertIn("update-fetch[", line)
            self.assertIn("error reason=download_http reachable=true curl_exit=22 http=503", line)
            # Details are sanitised the same way as check-status.
            self.assertIn("detail=HTTP 503 from URL", line)


class InstallerLogTests(unittest.TestCase):
    def run_installer(self, *args: str) -> Path:
        directory = tempfile.mkdtemp(prefix="ota-log-install-")
        self.addCleanup(subprocess.run, ["rm", "-rf", directory])
        data = Path(directory) / "data"
        (data / "libreecho/update").mkdir(parents=True)
        script = Path(directory) / "libreecho-update"
        script.write_text(UPDATE.read_text().replace("DATA_ROOT=/data\n",
                                                     f"DATA_ROOT={data}\n", 1))
        result = subprocess.run([BUSYBOX, "sh", str(script), *args],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        return data / "libreecho/update/ota.log"

    def test_install_failure_is_logged(self) -> None:
        log = self.run_installer("install", "/nonexistent/package.tar")
        line = log.read_text().splitlines()[-1]
        self.assertRegex(line, LINE)
        self.assertIn("update[", line)
        self.assertIn("install-error reason=userdata_identity_or_mount_failed state=none", line)

    def test_inspect_failure_is_not_logged(self) -> None:
        # The fetcher inspects partial downloads; those failures are expected.
        log = self.run_installer("inspect", "/nonexistent/package.tar")
        self.assertFalse(log.exists())


class DataCleanupTests(unittest.TestCase):
    def test_ota_log_files_are_known_to_data_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-log-cleanup-") as directory:
            data = Path(directory) / "data"
            update = data / "libreecho/update"
            update.mkdir(parents=True)
            for name in ("ota.log", "ota.log.1"):
                (update / name).write_text("x\n")
            result = subprocess.run(
                ["/bin/sh", str(CLEANUP)],
                env={**os.environ, "LIBREECHO_DATA_TEST_MODE": "1", "DATA_ROOT": str(data)},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("ota.log", result.stderr)
            self.assertTrue((update / "ota.log").exists())
            self.assertTrue((update / "ota.log.1").exists())


if __name__ == "__main__":
    unittest.main()
