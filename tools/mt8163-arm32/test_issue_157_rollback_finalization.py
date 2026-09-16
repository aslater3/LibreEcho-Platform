#!/usr/bin/env python3
"""Regression coverage for issue #157 stale OTA rollback finalisation."""
from __future__ import annotations

import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT = ROOT / "initramfs/libreecho-init"
UPDATER = ROOT / "initramfs/libreecho-update"


def extract_function(source: str, name: str) -> str:
    marker = f"{name}()\n{{"
    start = source.index(marker)
    end = source.index("\n}\n", start) + 3
    return source[start:end]


class RollbackFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-157-")
        self.root = Path(self.tmp.name)
        self.update = self.root / "update"
        self.staging = self.update / "staging"
        self.staging.mkdir(parents=True)
        (self.update / "pending").write_text(
            "schema=2\nstate=pending\nslot=b\nversion=0.14.0\n"
            "update_channel=dev\ntransaction_id=issue-157-candidate\n"
        )
        (self.update / "feature-commit").write_text(
            "schema=2\nphase=prepared\ntransaction_id=issue-157-candidate\n"
        )
        (self.staging / "manifest").write_bytes(b"signed-candidate-manifest\n")
        (self.staging / "manifest.sig").write_bytes(b"signed-candidate-signature\n")
        (self.update / "state").write_text(
            "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:services-ready\n"
        )
        (self.update / "check-status").write_text(
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=error\nsource_reachable=true\nlatest_version=0.14.0\n"
            "last_check_epoch=123\nlast_success_epoch=100\n"
            "error=health-confirm-failed\nerror_exit=\nerror_detail=stale\nhttp_status=200\n"
        )
        (self.update / "automatic-updates").write_text("channel=dev\n")
        (self.update / "restart-record").write_text(
            "schema=1\nreason=ota-health-confirm-failed\npending_slot=b\nselected_slot=b\n"
        )
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("console=tty0 androidboot.slot_suffix=_a\n")
        self.packaged_channel = self.root / "packaged-channel"
        self.packaged_channel.write_text("dev\n")
        self.bootctl = self.root / "bootctl"
        self.transaction = self.root / "feature-transaction"
        self.log = self.root / "init.log"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_bootctl(self, *, selected: str = "a", success: int = 1) -> None:
        self.bootctl.write_text(
            "#!/bin/sh\n"
            "[ \"${1:-}\" = status ] || exit 2\n"
            f"echo selected_slot={selected}\n"
            f"echo slot_{selected}_success={success}\n"
            "exit 0\n"
        )
        self.bootctl.chmod(0o755)

    def write_transaction(self, *, succeeds: bool) -> None:
        called = self.root / "transaction-called"
        if succeeds:
            body = (
                "[ \"${1:-}\" = fallback ] || exit 2\n"
                f"cp {shlex.quote(str(self.update / 'pending'))} {shlex.quote(str(self.update / 'rolled-back'))} || exit 3\n"
                f"rm -f {shlex.quote(str(self.update / 'pending'))} {shlex.quote(str(self.update / 'feature-commit'))}\n"
                f"rm -rf {shlex.quote(str(self.staging))}\n"
                "exit 0\n"
            )
        else:
            body = "[ \"${1:-}\" = fallback ] || exit 2\nexit 41\n"
        self.transaction.write_text(
            "#!/bin/sh\n"
            f": > {shlex.quote(str(called))}\n"
            + body
        )
        self.transaction.chmod(0o755)

    def fixture(self) -> Path:
        source = INIT.read_text()
        functions = extract_function(source, "publish_rolled_back_state")
        functions += extract_function(source, "finalize_stale_ota_fallback")
        replacements = {
            "rollback_update_root=/data/libreecho/update":
                f"rollback_update_root={shlex.quote(str(self.update))}",
            "rollback_transaction=/usr/local/sbin/libreecho-feature-transaction":
                f"rollback_transaction={shlex.quote(str(self.transaction))}",
            "rollback_bootctl=/usr/local/sbin/libreecho-bootctl":
                f"rollback_bootctl={shlex.quote(str(self.bootctl))}",
            "rollback_cmdline=/proc/cmdline":
                f"rollback_cmdline={shlex.quote(str(self.cmdline))}",
            "rollback_packaged_channel=/etc/libreecho/update-channel":
                f"rollback_packaged_channel={shlex.quote(str(self.packaged_channel))}",
        }
        for old, new in replacements.items():
            self.assertIn(old, functions, old)
            functions = functions.replace(old, new)
        script = self.root / "finalize-fixture"
        script.write_text(
            "#!/bin/busybox sh\n"
            "BB=/bin/busybox\n"
            f"log() {{ printf '%s\\n' \"$*\" >> {shlex.quote(str(self.log))}; }}\n"
            "pmsg_marker() { :; }\n"
            + functions
            + "\nfinalize_stale_ota_fallback\n"
        )
        script.chmod(0o755)
        return script

    def run_fixture(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/busybox", "sh", str(self.fixture())],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_confirmed_fallback_finalises_before_new_update_state(self) -> None:
        self.write_bootctl(selected="a", success=1)
        self.write_transaction(succeeds=True)
        restart_before = (self.update / "restart-record").read_bytes()
        result = self.run_fixture()
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr, self.log.read_text()))
        self.assertFalse((self.update / "pending").exists())
        self.assertFalse((self.update / "feature-commit").exists())
        self.assertFalse(self.staging.exists())
        self.assertTrue((self.update / "rolled-back").is_file())
        self.assertEqual((self.update / "restart-record").read_bytes(), restart_before)
        self.assertEqual(
            (self.update / "state").read_text(),
            "schema=1\nstate=rolled-back\nprogress=100\ndetail=b:a\n",
        )
        check = (self.update / "check-status").read_text()
        self.assertIn("channel=dev\n", check)
        self.assertIn("status=not-checked\n", check)
        self.assertIn("source_reachable=unknown\n", check)
        self.assertIn("last_check_epoch=0\n", check)
        self.assertIn("last_success_epoch=100\n", check)
        self.assertIn("error=\n", check)
        self.assertNotIn("health-confirm-failed", check)

    def test_helper_rejection_preserves_signed_transaction_evidence(self) -> None:
        self.write_bootctl(selected="a", success=1)
        self.write_transaction(succeeds=False)
        pending_before = (self.update / "pending").read_bytes()
        journal_before = (self.update / "feature-commit").read_bytes()
        manifest_before = (self.staging / "manifest").read_bytes()
        signature_before = (self.staging / "manifest.sig").read_bytes()
        state_before = (self.update / "state").read_bytes()
        check_before = (self.update / "check-status").read_bytes()
        result = self.run_fixture()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.update / "pending").read_bytes(), pending_before)
        self.assertEqual((self.update / "feature-commit").read_bytes(), journal_before)
        self.assertEqual((self.staging / "manifest").read_bytes(), manifest_before)
        self.assertEqual((self.staging / "manifest.sig").read_bytes(), signature_before)
        self.assertEqual((self.update / "state").read_bytes(), state_before)
        self.assertEqual((self.update / "check-status").read_bytes(), check_before)

    def test_unconfirmed_selected_slot_fails_closed_without_transaction(self) -> None:
        self.write_bootctl(selected="a", success=0)
        self.write_transaction(succeeds=True)
        result = self.run_fixture()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "transaction-called").exists())
        self.assertTrue((self.update / "pending").exists())
        self.assertTrue((self.update / "feature-commit").exists())

    def test_source_orders_finalisation_before_services_and_protects_staging(self) -> None:
        source = INIT.read_text()
        invocation = "if ! finalize_stale_ota_fallback; then"
        self.assertIn(invocation, source)
        self.assertLess(source.index(invocation), source.index("start_ui_services &"))
        updater = UPDATER.read_text()
        self.assertIn("transaction_pending && die transaction_pending", updater)


if __name__ == "__main__":
    unittest.main()
