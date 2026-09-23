#!/usr/bin/env python3
"""Regression contracts for the 0.14 OTA rollback investigated in issue #169."""
from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
INIT = HERE / "initramfs/libreecho-init"
RUNTIME = HERE / "mdns/runtime-contract.json"


class Issue169HealthContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.init = INIT.read_text()
        self.runtime = json.loads(RUNTIME.read_text())

    def test_health_uses_host_visible_mdns_runtime(self) -> None:
        root = "/" + self.runtime["image_runtime_root"]
        dirs = self.runtime["runtime_dirs"]
        self.assertEqual(dirs["state_root"], root)
        self.assertEqual(dirs["bus"], root + "/run/dbus")
        self.assertEqual(dirs["services"], root + "/etc/avahi/services")
        self.assertIn(
            '[ -S "$MDNS_RUNTIME_ROOT/run/dbus/system_bus_socket" ] || return 1',
            self.init,
        )
        self.assertNotIn(
            "[ -S /run/libreecho/mdns/dbus/system_bus_socket ] || return 1",
            self.init,
        )

    def test_ota_attempts_start_only_after_startup_ready(self) -> None:
        wait = self.init.index("log ota-health-waiting-startup-ready")
        loop = self.init.index('while [ "$attempt" -lt 6 ] && [ "$passed" -lt 3 ]')
        self.assertLess(wait, loop)
        self.assertIn('while ! startup_ready_marker_valid && [ "$startup_wait" -lt 180 ]', self.init)
        self.assertIn("last_check=startup-ready", self.init)
        self.assertNotIn("# before making the slot permanent.\n    $BB sleep 45", self.init)

    def test_pstore_and_dynamic_pmsg_are_prepared(self) -> None:
        self.assertIn("setup_pstore()", self.init)
        self.assertIn('$BB mount -t pstore pstore "$pstore_dir"', self.init)
        self.assertIn("/sys/class/pmsg/pmsg0/dev", self.init)
        self.assertIn('$BB mknod /dev/pmsg0 c "$major" "$minor"', self.init)
        self.assertNotRegex(self.init, r"mknod /dev/pmsg0 c [0-9]+ [0-9]+")

    def test_pmsg_is_bounded_and_only_selected_markers_are_mirrored(self) -> None:
        self.assertIn("pmsg_marker()", self.init)
        self.assertIn('[ "${#marker}" -le 160 ] || return 0', self.init)
        self.assertIn("pmsg_marker mdns-health-not-ready", self.init)
        self.assertIn('pmsg_marker "ota-probe-failed-$last_check"', self.init)
        self.assertIn('pmsg_marker "ota-confirm-failed-$last_check"', self.init)
        # General log() output can contain board identity, URLs or configuration
        # and therefore must never be copied wholesale to pmsg.
        log_start = self.init.index("\nlog()\n{")
        log_end = self.init.index("\n}\n", log_start) + len("\n}\n")
        log_block = self.init[log_start:log_end]
        self.assertNotIn("pmsg", log_block)


def shell_function(text: str, name: str) -> str:
    """Extract a real shell function so a test can execute it as shipped."""
    start = text.index(f"\n{name}()\n{{\n") + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class OtaFailureEvidenceContracts(unittest.TestCase):
    """Why a candidate slot rolled back must survive the reboot.

    A failed candidate is rebooted by the health worker, which destroys /tmp and
    with it /tmp/init.log -- the only place the underlying reason was written.
    These contracts keep that reason on the persistent filesystem.
    """

    def setUp(self) -> None:
        self.init = INIT.read_text()

    def test_failure_evidence_is_persistent_bounded_and_atomic(self) -> None:
        body = shell_function(self.init, "persist_failure_log")
        # Persistent userdata, never tmpfs: /tmp dies with the very reboot the
        # record exists to explain.
        self.assertIn('"${FAILURE_LOG_DIR:=/data/libreecho/update}"', body)
        self.assertIn('"${FAILURE_LOG_MAX_BYTES:=8192}"', body)
        self.assertIn('"${FAILURE_LOG_KEEP:=3}"', body)
        # Atomic and durable, mirroring the existing restart-record contract.
        self.assertIn("$BB chmod 0600", body)
        self.assertIn('$BB mv "$tmp" "$target"', body)
        self.assertIn("$BB sync", body)
        # Recording evidence must never change the boot outcome.
        self.assertIn("return 0", body)

    def test_failure_evidence_is_never_mirrored_to_pmsg(self) -> None:
        for name in ("persist_failure_log", "sanitize_failure_text"):
            self.assertNotIn("pmsg", shell_function(self.init, name))

    def test_feature_activation_reason_is_captured(self) -> None:
        # The transaction tool's fail() reason is its stderr; without capturing
        # it the sub-step is lost to the ramdisk.
        self.assertIn(
            'if ! activation_error=$("$transaction" activate-mounts 2>&1); then',
            self.init,
        )
        self.assertIn(
            'persist_failure_log feature-activation-rejected "$activation_error"',
            self.init,
        )

    def test_health_failure_persists_evidence_before_reboot(self) -> None:
        persist = self.init.index(
            'persist_failure_log ota-health-confirm-failed "$last_check"'
        )
        marker = self.init.index("pmsg_marker ota-rebooting-after-health-failure")
        # The reboot that matters is the one in the health-failure path, not any
        # earlier reboot call elsewhere in init.
        reboot = self.init.index("$BB reboot -f", marker)
        self.assertLess(persist, marker)
        self.assertLess(marker, reboot)
        # The restart record points at the detail record.
        self.assertIn('echo "failure_log=${failure_log:-none}"', self.init)

    def test_failure_log_scrubs_secrets_and_rotates(self) -> None:
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_log = root / "init.log"
            init_log.write_text(
                "line one\n"
                "https://github.com/aslater3/LibreEcho/releases/download/secret-tag/a.tar\n"
                "wlan0 bssid 00:11:22:33:44:55\n"
                "ssid:PrivateNetworkName\n"
                "serialno=FAKESERIAL000001\n"
            )
            outdir = root / "update"
            outdir.mkdir()
            for index in range(4):
                stale = outdir / f"failure-{index}.log"
                stale.write_text("old\n")
                os.utime(stale, (1000 + index, 1000 + index))
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"INIT_LOG={shlex.quote(str(init_log))}\n"
                "FAILURE_LOG_MAX_BYTES=4096\n"
                "FAILURE_LOG_KEEP=2\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + 'persist_failure_log feature-activation-rejected "ERROR:activation-mount"\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = result.stdout.strip()
            self.assertRegex(written, r"^failure-[0-9]+-[0-9]+\.log$")
            record_path = outdir / written
            record = record_path.read_text()
            self.assertIn("reason=feature-activation-rejected", record)
            self.assertIn("ERROR:activation-mount", record)
            for secret in (
                "secret-tag",
                "00:11:22:33:44:55",
                "PrivateNetworkName",
                "FAKESERIAL000001",
            ):
                self.assertNotIn(secret, record)
            self.assertIn("URL", record)
            self.assertIn("MAC", record)
            self.assertIn("ssid:SSID", record)
            self.assertIn("serialno=SERIAL", record)
            self.assertLessEqual(record_path.stat().st_size, 4096)
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            kept = sorted(path.name for path in outdir.glob("failure-*.log"))
            self.assertEqual(len(kept), 2, kept)
            self.assertIn(written, kept)


if __name__ == "__main__":
    unittest.main()
