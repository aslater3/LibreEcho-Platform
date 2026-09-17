#!/usr/bin/env python3
"""Regression contracts for the automatic rollback finalization of issue #157.

Once the bootloader returns to the previously confirmed slot, the failed
candidate's transaction must be retired automatically *and* the device must
stop reporting the failed candidate's last health check.  These tests cover
both halves: the source-order contract in ``libreecho-init`` and the real
filesystem behaviour of the two extracted finalization helpers, executed on the
host against a synthetic update root.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INIT = HERE / "initramfs/libreecho-init"
BUSYBOX = "/bin/busybox"
UPDATE_ROOT = "/data/libreecho/update"

WORKER = "ota_health_confirm_worker"


def extract_function(text: str, name: str) -> str:
    """Return the exact ``    name()`` function body used inside the worker."""
    pattern = re.compile(rf"^    {re.escape(name)}\(\)\n    \{{\n.*?^    \}}\n", re.M | re.S)
    match = pattern.search(text)
    if match is None:
        raise AssertionError(f"function not found in libreecho-init: {name}")
    return match.group(0)


def worker_body(text: str) -> str:
    start = text.index(f"{WORKER}()")
    end = text.index("\n}", start) + 2
    return text[start:end]


class RollbackFinalizationSourceContracts(unittest.TestCase):
    """Static contracts on the boot worker that performs the finalization."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.init = INIT.read_text()
        cls.worker = worker_body(cls.init)

    def test_rollback_publishes_terminal_state_and_refreshes_check_status(self) -> None:
        publish = extract_function(self.init, "ota_rollback_publish_terminal")
        # The terminal rollback record the status surface already understands.
        self.assertIn("echo state=rolled-back", publish)
        self.assertIn("echo progress=100", publish)
        self.assertIn('echo "detail=$rollback_failed_slot"', publish)
        # Atomic publication: a staging file is renamed, then synced.
        self.assertIn("state.tmp", publish)
        self.assertLess(publish.index("state.tmp"), publish.index("$BB mv"))
        self.assertIn("$BB sync", publish)
        # A completed rollback is not a pending reboot, so the frozen
        # `reboot-pending` check record must be refreshed -- but only for the
        # version that actually rolled back.
        self.assertIn("status=reboot-pending", publish)
        self.assertIn("status=update-held-after-rollback", publish)
        self.assertIn("s/^latest_version=//p", publish)
        self.assertIn('= "$rollback_version"', publish)
        self.assertIn("/data/libreecho/update/rolled-back", publish)

    def test_v2_finalization_verifies_postconditions_before_claiming_cleanup(self) -> None:
        confirm = extract_function(self.init, "ota_v2_fallback_confirmed")
        for live_record in ("pending", "feature-commit", "staging", "rolled-back"):
            self.assertIn(live_record, confirm)
        # Three fail-closed refusals and one success assertion, in that order.
        self.assertEqual(confirm.count("return 1"), 3)
        self.assertTrue(confirm.rstrip().endswith("return 0\n    }"))
        self.assertNotIn("return 1\n", confirm[confirm.rindex("return 0"):])

    def test_boot_worker_runs_confirmation_and_publication_before_reporting_clean(self) -> None:
        fallback = self.worker.index("libreecho-feature-transaction fallback &&")
        confirm = self.worker.index("ota_v2_fallback_confirmed &&")
        publish = self.worker.index('ota_rollback_publish_terminal "$pending_slot"')
        cleaned = self.worker.index('log "ota-v2-fallback-cleaned:')
        preserved = self.worker.index("log ota-v2-fallback-preserved-for-recovery")
        self.assertLess(fallback, confirm)
        self.assertLess(confirm, publish)
        self.assertLess(publish, cleaned)
        # A refusal still leaves every record in place for operator recovery.
        self.assertLess(cleaned, preserved)

    def test_both_rollback_branches_share_one_terminal_publication(self) -> None:
        publish = extract_function(self.init, "ota_rollback_publish_terminal")
        # The v2 branch must not keep a private copy of the terminal writer:
        # the only state writer in the worker is the shared helper.
        self.assertEqual(self.worker.count("echo state=rolled-back"), 1)
        self.assertIn("echo state=rolled-back", publish)
        self.assertEqual(self.worker.count('ota_rollback_publish_terminal "$pending_slot"'), 2)
        schema_one = self.worker.index("mv /data/libreecho/update/pending")
        self.assertLess(schema_one, self.worker.index('log "ota-rollback-complete:'))


class _Fixture:
    """A disposable update root plus a runnable finalization script."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.update = root / "update"
        self.update.mkdir(parents=True)
        self.log = root / "finalize.log"

    def script(self, init_text: str) -> Path:
        body = (
            extract_function(init_text, "ota_rollback_publish_terminal")
            + extract_function(init_text, "ota_v2_fallback_confirmed")
        ).replace(UPDATE_ROOT, str(self.update))
        path = self.root / "finalize.sh"
        path.write_text(
            "#!/bin/busybox sh\n"
            f"BB={BUSYBOX}\n"
            f'log() {{ printf \'%s\\n\' "$*" >> {self.log}; }}\n'
            f"{body}\n"
            "if ota_v2_fallback_confirmed; then\n"
            "    printf 'CONFIRMED=0\\n'\n"
            '    ota_rollback_publish_terminal "$1"\n'
            "    printf 'PUBLISHED=%s\\n' \"$?\"\n"
            "else\n"
            "    printf 'CONFIRMED=1\\n'\n"
            "    printf 'PUBLISHED=skipped\\n'\n"
            "fi\n"
        )
        path.chmod(0o755)
        return path

    def run(self, init_text: str, failed_slot: str = "b") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BUSYBOX, "sh", str(self.script(init_text)), failed_slot],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )

    def write(self, name: str, text: str) -> None:
        (self.update / name).write_text(text)

    def read(self, name: str) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in (self.update / name).read_text().splitlines()
            if "=" in line
        )

    def markers(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


@unittest.skipUnless(os.path.exists(BUSYBOX), "busybox is required for the host fixture")
class RollbackFinalizationBehaviour(unittest.TestCase):
    """Disk-backed execution of the extracted helpers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.init = INIT.read_text()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="libreecho-rollback-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fx = _Fixture(self.tmp)

    def seed_finalized_rollback(self, latest_status: str = "reboot-pending",
                                version: str = "0.14.99") -> None:
        """The confirmed fallback slot as it appears once the version-matched
        recovery helper has retired the live transaction."""
        for stale in ("pending", "feature-commit"):
            path = self.fx.update / stale
            if path.exists():
                path.unlink()
        shutil.rmtree(self.fx.update / "staging", ignore_errors=True)
        self.fx.write(
            "rolled-back",
            f"schema=2\ntransaction_id=deadbeef\nversion={version}\nslot=b\n",
        )
        self.fx.write(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            f"status={latest_status}\nsource_reachable=true\n"
            f"latest_version={version}\nlast_check_epoch=1\nlast_success_epoch=0\n",
        )
        self.fx.write(
            "state",
            "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:web-status\n",
        )

    def test_confirmed_finalization_retires_live_records_and_publishes_terminal_state(self) -> None:
        self.seed_finalized_rollback()
        result = self.fx.run(self.init)
        self.assertIn("CONFIRMED=0", result.stdout, result.stderr)
        self.assertIn("PUBLISHED=0", result.stdout, result.stderr)
        # The live transaction is gone and the rollback record is retained.
        self.assertFalse((self.fx.update / "pending").exists())
        self.assertFalse((self.fx.update / "feature-commit").exists())
        self.assertFalse((self.fx.update / "staging").exists())
        self.assertTrue((self.fx.update / "rolled-back").is_file())
        # The public state is terminal instead of the failed candidate's
        # health-confirm restart record.
        state = self.fx.read("state")
        self.assertEqual(state["state"], "rolled-back")
        self.assertEqual(state["progress"], "100")
        self.assertEqual(state["detail"], "b")
        # A frozen `reboot-pending` check record becomes the rollback hold.
        check = self.fx.read("check-status")
        self.assertEqual(check["status"], "update-held-after-rollback")
        self.assertEqual(check["latest_version"], "0.14.99")
        self.assertEqual(check["last_check_epoch"], "1")
        self.assertFalse((self.fx.update / "state.tmp").exists())
        self.assertFalse((self.fx.update / "check-status.tmp").exists())

    def test_check_record_of_a_different_candidate_is_never_relabelled(self) -> None:
        self.seed_finalized_rollback()
        self.fx.write(
            "check-status",
            "schema=1\nstatus=reboot-pending\nlatest_version=0.15.0\n",
        )
        result = self.fx.run(self.init)
        self.assertIn("PUBLISHED=0", result.stdout, result.stderr)
        self.assertIn("status=reboot-pending", (self.fx.update / "check-status").read_text())

    def test_already_current_check_record_is_left_alone(self) -> None:
        self.seed_finalized_rollback(latest_status="up-to-date")
        result = self.fx.run(self.init)
        self.assertIn("PUBLISHED=0", result.stdout, result.stderr)
        self.assertIn("status=up-to-date", (self.fx.update / "check-status").read_text())

    def test_publication_is_idempotent_across_repeated_boots(self) -> None:
        self.seed_finalized_rollback()
        self.assertIn("PUBLISHED=0", self.fx.run(self.init).stdout)
        first_state = (self.fx.update / "state").read_bytes()
        first_check = (self.fx.update / "check-status").read_bytes()
        self.assertIn("PUBLISHED=0", self.fx.run(self.init).stdout)
        self.assertEqual((self.fx.update / "state").read_bytes(), first_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), first_check)

    def test_remaining_live_evidence_fails_closed_without_terminal_state(self) -> None:
        for leftover, content in (
            ("pending", "schema=2\nversion=0.14.99\nslot=b\n"),
            ("feature-commit", "phase=prepared\ntransaction_id=deadbeef\n"),
        ):
            with self.subTest(leftover=leftover):
                self.seed_finalized_rollback()
                self.fx.write(leftover, content)
                original_state = (self.fx.update / "state").read_bytes()
                result = self.fx.run(self.init)
                self.assertIn("CONFIRMED=1", result.stdout, result.stderr)
                self.assertIn("PUBLISHED=skipped", result.stdout, result.stderr)
                self.assertIn(
                    f"ota-v2-fallback-live-evidence-remains:{leftover}",
                    self.fx.markers(),
                )
                self.assertEqual((self.fx.update / "state").read_bytes(), original_state)

    def test_retained_staging_tree_fails_closed(self) -> None:
        self.seed_finalized_rollback()
        (self.fx.update / "staging").mkdir()
        (self.fx.update / "staging" / "manifest").write_text("format=libreecho-ota-v2\n")
        original_state = (self.fx.update / "state").read_bytes()
        result = self.fx.run(self.init)
        self.assertIn("CONFIRMED=1", result.stdout, result.stderr)
        self.assertIn("ota-v2-fallback-staging-remains", self.fx.markers())
        self.assertEqual((self.fx.update / "state").read_bytes(), original_state)

    def test_missing_rollback_evidence_fails_closed(self) -> None:
        self.seed_finalized_rollback()
        (self.fx.update / "rolled-back").unlink()
        original_state = (self.fx.update / "state").read_bytes()
        result = self.fx.run(self.init)
        self.assertIn("CONFIRMED=1", result.stdout, result.stderr)
        self.assertIn("ota-v2-fallback-history-missing", self.fx.markers())
        self.assertEqual((self.fx.update / "state").read_bytes(), original_state)

    def test_v1_pending_record_is_finalized_by_the_same_publication(self) -> None:
        self.fx.write("rolled-back", "schema=1\nversion=0.13.15\nslot=b\n")
        self.fx.write("state", "schema=1\nstate=restarting\nprogress=0\n")
        self.fx.write(
            "check-status",
            "schema=1\nstatus=reboot-pending\nlatest_version=0.13.15\n",
        )
        result = self.fx.run(self.init, failed_slot="a")
        self.assertIn("PUBLISHED=0", result.stdout, result.stderr)
        state = self.fx.read("state")
        self.assertEqual(state["state"], "rolled-back")
        self.assertEqual(state["detail"], "a")
        self.assertEqual(self.fx.read("check-status")["status"], "update-held-after-rollback")


if __name__ == "__main__":
    unittest.main()
