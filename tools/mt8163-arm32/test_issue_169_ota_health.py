#!/usr/bin/env python3
"""Regression contracts for the 0.14 OTA rollback investigated in issue #169."""
from pathlib import Path
import json
import os
import re
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

    def test_failure_log_records_slot_identity_and_bcb_readback(self) -> None:
        """The snapshot must carry what distinguishes a consumed final attempt."""
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "update"
            outdir.mkdir()
            (outdir / "pending").write_text(
                "schema=2\nslot=a\ntransaction_id=txn-test\n"
            )
            cmdline = root / "cmdline"
            cmdline.write_text("console=ttyS0 androidboot.slot_suffix=_a quiet\n")
            bootctl = root / "bootctl"
            bootctl.write_text(
                "#!/bin/sh\n"
                "printf 'schema=1\\nselected_slot=b\\ninactive_slot=a\\n"
                "slot_a_priority=15\\nslot_a_tries=0\\nslot_a_success=0\\n"
                "slot_b_priority=14\\nslot_b_tries=0\\nslot_b_success=1\\n'\n"
            )
            bootctl.chmod(0o755)
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"LIBREECHO_CMDLINE_FILE={shlex.quote(str(cmdline))}\n"
                f"LIBREECHO_BOOTCTL_TOOL={shlex.quote(str(bootctl))}\n"
                f"INIT_LOG={shlex.quote(str(root / 'init.log'))}\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + 'persist_failure_log feature-activation-rejected "ERROR:activation-bcb-slot"\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = result.stdout.strip()
            self.assertRegex(written, r"^failure-[0-9]+-[0-9]+\.log$")
            record = (outdir / written).read_text()
            self.assertIn("reason=feature-activation-rejected", record)
            self.assertIn("running_slot=a", record)
            self.assertIn("pending_slot=a", record)
            self.assertIn("transaction_id=txn-test", record)
            self.assertIn("--- bcb-readback", record)
            # The bare readback names the other slot: the BCB's next-boot
            # selection, which is exactly the state that rejected the candidate.
            self.assertIn("selected_slot=b", record)
            self.assertIn("slot_a_tries=0", record)
            self.assertIn("ERROR:activation-bcb-slot", record)

    def test_failure_log_marks_a_missing_bootctl_rather_than_failing(self) -> None:
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "update"
            outdir.mkdir()
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"LIBREECHO_BOOTCTL_TOOL={shlex.quote(str(root / 'absent-bootctl'))}\n"
                f"LIBREECHO_CMDLINE_FILE={shlex.quote(str(root / 'absent-cmdline'))}\n"
                f"INIT_LOG={shlex.quote(str(root / 'init.log'))}\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + 'persist_failure_log ota-health-confirm-failed "startup-ready"\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = (outdir / result.stdout.strip()).read_text()
            self.assertIn("bcb-readback-unavailable", record)
            self.assertIn("pending_slot=none", record)
            self.assertIn("transaction_id=none", record)


    def test_failure_log_captures_failed_airplay_reconcile_output(self) -> None:
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "update"
            outdir.mkdir()
            init_log = root / "init.log"
            init_log.write_text("feature-reconcile-service-start-failed:airplayd:1\n")
            airplay_log = root / "airplayd.reconcile.log"
            airplay_log.write_text(
                "libreecho-airplayd: shared D-Bus socket unavailable\n"
                'libreecho-airplayd: AirPlay service name is "Kitchen Speaker"\n'
                "https://example.invalid/path?token=PRIVATE\n"
                "ssid:PrivateNetworkName serialno=FAKESERIAL000001 "
                "00:11:22:33:44:55\n"
            )
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"INIT_LOG={shlex.quote(str(init_log))}\n"
                "FAILURE_LOG_MAX_BYTES=4096\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + f'persist_failure_log ota-health-confirm-failed "startup-ready" {shlex.quote(str(airplay_log))}\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record_path = outdir / result.stdout.strip()
            record = record_path.read_text()
            self.assertIn("--- airplayd-reconcile-log", record)
            self.assertIn("shared D-Bus socket unavailable", record)
            for secret in (
                "Kitchen Speaker", "PRIVATE", "PrivateNetworkName",
                "FAKESERIAL000001", "00:11:22:33:44:55",
            ):
                self.assertNotIn(secret, record)
            self.assertIn("URL", record)
            self.assertIn("ssid:SSID", record)
            self.assertIn("serialno=SERIAL", record)
            self.assertIn("MAC", record)
            self.assertLessEqual(record_path.stat().st_size, 4096)
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)

    def test_failure_log_reads_the_transaction_journal_under_its_real_name(self) -> None:
        """The journal is $ROOT/feature-commit, not a file named "journal"."""
        body = shell_function(self.init, "persist_failure_log")
        self.assertIn("feature-commit", body)
        self.assertNotIn('FAILURE_LOG_DIR/journal', body)
        # The id is taken from the journal first, then from the pending record,
        # which carries the same field while the candidate is still pending.
        # Compare by read order rather than by literal text: both reads are
        # line-wrapped, so an exact quoted string is brittle. Anchor on the
        # path each sed expression reads.
        flat = re.sub(r"\\\s*\n\s*", " ", body)
        # Anchor on the path each sed expression reads; the indentation left by
        # the wrap is not stable, so do not match the whole command text.
        self.assertLess(
            flat.index("FAILURE_LOG_DIR/feature-commit"),
            flat.index("FAILURE_LOG_DIR/pending", flat.index("transaction_id=$($BB sed -n 's/^transaction_id=//p'")),
        )

    def test_failure_log_keeps_identity_when_only_the_journal_exists(self) -> None:
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "update"
            outdir.mkdir()
            # The rollback path can leave the journal behind without pending;
            # the id must still be recovered from the journal alone.
            (outdir / "feature-commit").write_text(
                "schema=2\ntransaction_id=txn-journal-only\n"
            )
            bootctl = root / "bootctl"
            bootctl.write_text("#!/bin/sh\nprintf 'selected_slot=b\\n'\n")
            bootctl.chmod(0o755)
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"LIBREECHO_BOOTCTL_TOOL={shlex.quote(str(bootctl))}\n"
                f"LIBREECHO_CMDLINE_FILE={shlex.quote(str(root / 'absent-cmdline'))}\n"
                f"INIT_LOG={shlex.quote(str(root / 'init.log'))}\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + 'persist_failure_log feature-activation-rejected "ERROR:activation-bcb-slot"\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = (outdir / result.stdout.strip()).read_text()
            self.assertIn("transaction_id=txn-journal-only", record)
            self.assertIn("pending_slot=none", record)

    def test_failure_log_keeps_identity_and_readback_under_truncation(self) -> None:
        """A huge detail must not push out the identity or the readback."""
        busybox = shutil.which("busybox") or ""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "update"
            outdir.mkdir()
            (outdir / "pending").write_text("schema=2\nslot=a\ntransaction_id=txn-t\n")
            bootctl = root / "bootctl"
            bootctl.write_text(
                "#!/bin/sh\nprintf 'selected_slot=b\\nslot_a_tries=0\\n'\n"
            )
            bootctl.chmod(0o755)
            big = "ERROR:" + ("x" * 20000)
            script = (
                "set -eu\n"
                f"BB={shlex.quote(busybox)}\n"
                f"FAILURE_LOG_DIR={shlex.quote(str(outdir))}\n"
                f"LIBREECHO_BOOTCTL_TOOL={shlex.quote(str(bootctl))}\n"
                f"LIBREECHO_CMDLINE_FILE={shlex.quote(str(root / 'absent-cmdline'))}\n"
                f"INIT_LOG={shlex.quote(str(root / 'init.log'))}\n"
                "FAILURE_LOG_MAX_BYTES=4096\n"
                + shell_function(self.init, "sanitize_failure_text")
                + shell_function(self.init, "persist_failure_log")
                + f'persist_failure_log feature-activation-rejected {shlex.quote(big)}\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record_path = outdir / result.stdout.strip()
            record = record_path.read_text()
            self.assertLessEqual(record_path.stat().st_size, 4096)
            # Identity and readback come first, so they survive the cap.
            self.assertIn("transaction_id=txn-t", record)
            self.assertIn("selected_slot=b", record)
            self.assertIn("slot_a_tries=0", record)


if __name__ == "__main__":
    unittest.main()
