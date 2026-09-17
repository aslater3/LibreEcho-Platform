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
import signal
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

    def test_boot_worker_retries_an_interrupted_publication_on_every_boot(self) -> None:
        # The interruption the recovery path exists for: the finalized rollback
        # record survives, the live transaction does not, and the terminal
        # records were never written.
        resume = extract_function(self.init, "ota_rollback_resume_terminal")
        self.assertIn("/data/libreecho/update/pending", resume)
        self.assertIn("/data/libreecho/update/feature-commit", resume)
        self.assertIn("s/^state=//p", resume)
        self.assertIn("detail=//p", resume)
        self.assertIn("s/^slot=//p", resume)
        self.assertIn('ota_rollback_publish_terminal "$resume_slot"', resume)
        # The cleanup post-condition the rollback branch asserts is required
        # here too, or an interrupted staging cleanup would be published as a
        # finished rollback.
        self.assertIn("/data/libreecho/update/staging", resume)
        self.assertIn("log ota-rollback-resume-cleanup-incomplete", resume)
        # Fail closed: a refused publication is retried, not forced, and the
        # slot the history record names is the only one that may be published.
        self.assertIn("log ota-rollback-resume-evidence-invalid", resume)
        self.assertIn("ota-rollback-terminal-publication-retry-queued", resume)
        # The retry must run on every boot of an OTA image, before the boot
        # decides whether a live transaction exists at all -- reachable only
        # from one branch is the bug this path fixes.
        call = self.worker.index("ota_rollback_resume_terminal\n")
        self.assertLess(call, self.worker.index("if [ -r /data/libreecho/update/pending ]"))
        self.assertLess(
            self.worker.index("ota_rollback_resume_terminal()"),
            self.worker.index("ota_rollback_resume_terminal\n"),
        )
        self.assertEqual(self.worker.count("\n    ota_rollback_resume_terminal\n"), 1)

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


# Absolute roots the shipped boot worker touches.  They are redirected into a
# sandbox in one pass so the boot decision under test is the shipped one.
BOOT_WORKER_ROOTS = (
    "/data/libreecho",
    "/usr/local/sbin",
    "/etc/init.d",
    "/var/run",
    "/run/libreecho",
    "/proc/cmdline",
    "/proc/mounts",
    "/tmp/",
)

ROLLBACK_VERSION = "0.14.99"
ROLLBACK_SLOT = "b"
FALLBACK_INTERRUPTED = "ota-v2-fallback-preserved-for-recovery"


class _BootWorkerFixture:
    """A disposable root filesystem that runs the real boot worker.

    ``ota_health_confirm_worker`` is lifted verbatim from the shipped PID 1
    script and every absolute path it uses is redirected into this sandbox, so
    the branch it takes, the records it writes and the retries it schedules are
    the shipped ones.  The packaged helpers it shells out to are stubs: this
    proves the Platform boot contract -- which records survive a boot, and when
    the worker republishes them -- not the rollback logic inside those helpers.
    ``reboot`` is stubbed as well, so a host test can never reboot the machine
    running it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.update = root / "data/libreecho/update"
        self.update.mkdir(parents=True)
        self.boot_log = root / "boot.log"
        self.reboots = root / "reboots.log"
        self.fallbacks = root / "fallbacks.log"
        for directory in (
            "usr/local/sbin", "etc/init.d", "etc/libreecho", "var/run",
            "run/libreecho", "proc", "tmp",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        # The slot the bootloader returned to and the per-slot success flags.
        (root / "proc/cmdline").write_text(
            "console=ttyMSM0 androidboot.slot_suffix=_a\n"
        )
        (root / "proc/mounts").write_text("")
        # The image's first-install marker is a build-time file, so it is
        # present and valid on every device that can be running a rollback.
        (root / "etc/libreecho/first-install-confirm").write_text(
            "schema=1\nmode=first-install\nboard=radar_puffin\n"
        )
        self.shim = self._write(
            root / "bb",
            "#!/bin/sh\n"
            "# BusyBox with the boot-mutating applets neutralised.\n"
            'case "${1:-}" in\n'
            f'    reboot) printf \'%s\\n\' "$*" >>"{self.reboots}"; exit 0 ;;\n'
            "    sleep) exit 0 ;;\n"
            "    sync) exit 0 ;;\n"
            "esac\n"
            f'exec "{BUSYBOX}" "$@"\n',
        )
        self._write(
            root / "usr/local/sbin/libreecho-bootctl",
            "#!/bin/sh\n"
            "# The previously confirmed slot is running and stays selected.\n"
            "printf 'selected_slot=a\\nslot_a_success=1\\nslot_b_success=0\\n'\n",
        )
        self._write(
            root / "usr/local/sbin/libreecho-update",
            "#!/bin/sh\n"
            'if [ "${1:-}" = status ]; then printf \'state=rolled-back\\n\'; fi\n'
            "exit 0\n",
        )
        self._write(
            root / "usr/local/sbin/libreecho-feature-transaction",
            "#!/bin/sh\n"
            "# Stub for the packaged recovery helper.  The shipped helper\n"
            "# records its fallback history, retires the pending record and the\n"
            "# feature commit, and only then removes its staging tree, so the\n"
            "# stub keeps that order: an interruption can be placed on either\n"
            "# side of the staging cleanup.\n"
            "set -u\n"
            f'update="{self.update}"\n'
            f'printf \'%s\\n\' "${{1:-}}" >>"{self.fallbacks}"\n'
            '[ "${1:-}" = fallback ] || exit 0\n'
            "printf 'schema=2\\ntransaction_id=deadbeef\\nversion=%s\\nslot=%s\\n' \\\n"
            f"    '{ROLLBACK_VERSION}' '{ROLLBACK_SLOT}' >\"$update/rolled-back\"\n"
            'rm -f "$update/pending" "$update/feature-commit"\n'
            'if [ "${INTERRUPT_BEFORE_STAGING_CLEANUP:-0}" = 1 ]; then\n'
            '    kill -KILL "$PPID"\n'
            "    exit 0\n"
            "fi\n"
            'rm -rf "$update/staging"\n'
            'if [ "${INTERRUPT_AFTER_CLEANUP:-0}" = 1 ]; then\n'
            '    kill -KILL "$PPID"\n'
            "fi\n"
            "exit 0\n",
        )
        self.harness = self._write(
            root / "worker.sh",
            "#!/bin/busybox sh\n"
            "set -u\n"
            f'BB="{self.shim}"\n'
            "IMAGE_PROFILE=ota\n"
            "FEATURE_POLICY=preserve\n"
            "SERVICE_PROFILE=production\n"
            f'FIRST_INSTALL_MARKER="{root}/etc/libreecho/first-install-confirm"\n'
            f'STARTUP_READY="{root}/run/libreecho/startup-ready"\n'
            "RUNTIME_ROOT=/run/libreecho/features\n"
            "MDNS_RUNTIME_ROOT=/usr/local/lib/libreecho-mdns/root\n"
            "NF=3\n"
            'INIT_TEST_LOG="${BOOT_LOG:?}"\n'
            'log() { printf \'%s\\n\' "$*" >>"$INIT_TEST_LOG"; }\n'
            "pmsg_marker() { :; }\n"
            + self._redirected_worker()
            + "\nota_health_confirm_worker\nexit $?\n",
        )

    def _write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def _redirected_worker(self) -> str:
        body = worker_body(INIT.read_text())
        # One pass: a replacement must never be rescanned, or the sandbox
        # prefix itself would be rewritten a second time.
        return re.sub(
            "|".join(re.escape(part) for part in BOOT_WORKER_ROOTS),
            lambda found: f"{self.root}{found.group(0)}",
            body,
        )

    def write_update(self, name: str, text: str) -> None:
        (self.update / name).write_text(text)

    def read_update(self, name: str) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in (self.update / name).read_text().splitlines()
            if "=" in line
        )

    def markers(self) -> str:
        return self.boot_log.read_text() if self.boot_log.exists() else ""

    def seed_failed_candidate(self) -> None:
        """The failed candidate's live transaction and the records it left."""
        self.write_update(
            "pending",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        self.write_update(
            "feature-commit", "phase=committed\ntransaction_id=deadbeef\n"
        )
        self.write_update(
            "state",
            "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:web-status\n",
        )
        self.write_update(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=reboot-pending\nsource_reachable=true\n"
            f"latest_version={ROLLBACK_VERSION}\nlast_check_epoch=1\n",
        )

    def seed_finalized_rollback(self, terminal_state: str = "rolled-back") -> None:
        """The device as the rollback left it: history record, frozen check
        record, and either the failed candidate's restart record or the
        terminal state that was published before the interruption."""
        self.write_update(
            "rolled-back",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        self.write_update(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=reboot-pending\nsource_reachable=true\n"
            f"latest_version={ROLLBACK_VERSION}\nlast_check_epoch=1\n",
        )
        self.write_update(
            "state",
            "schema=1\nstate=rolled-back\nprogress=100\ndetail=b\n"
            if terminal_state == "rolled-back"
            else "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:web-status\n",
        )

    def boot(self, interrupt_at: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run one boot of the shipped worker against the sandbox root.

        ``interrupt_at`` places the power loss inside the recovery helper:
        "staging" before its staging cleanup, "cleanup" once it is done, and
        ``None`` for a boot that completes.
        """
        return subprocess.run(
            [BUSYBOX, "sh", str(self.harness)],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "BOOT_LOG": str(self.boot_log),
                "INTERRUPT_BEFORE_STAGING_CLEANUP": (
                    "1" if interrupt_at == "staging" else "0"
                ),
                "INTERRUPT_AFTER_CLEANUP": "1" if interrupt_at == "cleanup" else "0",
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )

    def assert_cleanup_happened(self) -> None:
        for gone in ("pending", "feature-commit", "staging"):
            if (self.update / gone).exists():
                raise AssertionError(f"live record survived the rollback: {gone}")
        if not (self.update / "rolled-back").is_file():
            raise AssertionError("the finalized rollback record is missing")


@unittest.skipUnless(os.path.exists(BUSYBOX), "busybox is required for the host fixture")
class RollbackFinalizationResumesAfterInterruption(unittest.TestCase):
    """A finalized rollback must reach its terminal records across a reboot.

    The recovery helper retires the live transaction *before* the worker
    publishes the terminal status, so a power loss (or a failed write) in
    between leaves a device that no later boot can finish: the transaction the
    rollback branch retires is already gone.  These tests run the shipped boot
    worker itself, on boot after boot, instead of invoking its extracted
    publisher, so the recovery path -- not just the writer -- is what is proven.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.init = INIT.read_text()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="libreecho-boot-worker-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fx = _BootWorkerFixture(self.tmp)

    def assert_terminal_publication(self, markers: str) -> None:
        state = self.fx.read_update("state")
        self.assertEqual(state["schema"], "1")
        self.assertEqual(state["state"], "rolled-back")
        self.assertEqual(state["progress"], "100")
        self.assertEqual(state["detail"], ROLLBACK_SLOT)
        check = self.fx.read_update("check-status")
        self.assertEqual(check["status"], "update-held-after-rollback")
        self.assertEqual(check["latest_version"], ROLLBACK_VERSION)
        # The rewrite keeps the rest of the check record.
        self.assertEqual(check["last_check_epoch"], "1")
        self.assertEqual(check["channel"], "dev")
        self.assertFalse((self.fx.update / "state.tmp").exists())
        self.assertFalse((self.fx.update / "check-status.tmp").exists())
        self.assertIn(
            f"ota-rollback-terminal-publication-resumed:{ROLLBACK_SLOT}", markers
        )

    def assert_untouched_failed_candidate(self) -> None:
        state = self.fx.read_update("state")
        self.assertEqual(state["state"], "restarting")
        self.assertEqual(state["detail"], "health-confirm-failed:web-status")
        self.assertEqual(
            self.fx.read_update("check-status")["status"], "reboot-pending"
        )
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )

    def test_power_loss_after_cleanup_is_finished_by_the_next_boot(self) -> None:
        self.fx.seed_failed_candidate()
        # Boot 1: the rollback branch retires the transaction and the device
        # loses power before the terminal status is published.
        interrupted = self.fx.boot(interrupt_at="cleanup")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.fx.assert_cleanup_happened()
        self.assert_untouched_failed_candidate()

        # Boot 2: no live transaction is left, so only the worker's recovery
        # path can finish the publication.
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        # The cleanup ran exactly once: the terminal records were recovered
        # from the finalized history record, not by another rollback.
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        # Nothing in the recovery path reboots the device.
        self.assertFalse(self.fx.reboots.exists())

        # Boot 3: the published records are stable, so a later boot is a no-op.
        before = (self.fx.update / "state").read_bytes()
        terminal = (self.fx.update / "check-status").read_bytes()
        third = self.fx.boot()
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual((self.fx.update / "state").read_bytes(), before)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), terminal)
        self.assertFalse(self.fx.reboots.exists())

    def test_failed_terminal_publication_is_retried_on_the_next_boot(self) -> None:
        self.fx.seed_failed_candidate()
        # Boot 1: the cleanup succeeds but the state publication cannot be
        # written (the staging file is occupied), so the rollback branch keeps
        # the transaction retired and reports the refusal.
        (self.fx.update / "state.tmp").mkdir()
        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn(FALLBACK_INTERRUPTED, self.fx.markers())
        self.fx.assert_cleanup_happened()
        self.assert_untouched_failed_candidate()
        (self.fx.update / "state.tmp").rmdir()

        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])

    def test_interrupted_staging_cleanup_is_not_published_as_finalized(self) -> None:
        self.fx.seed_failed_candidate()
        # Boot 1: the helper records the fallback history and retires the
        # pending record and the feature commit, then the device loses power
        # before its staging tree is removed -- the boundary `fallback` leaves
        # when it is interrupted between those two steps.
        interrupted = self.fx.boot(interrupt_at="staging")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        for gone in ("pending", "feature-commit"):
            self.assertFalse((self.fx.update / gone).exists(), gone)
        self.assertTrue((self.fx.update / "rolled-back").is_file())
        self.assertTrue((self.fx.update / "staging").exists())
        self.assert_untouched_failed_candidate()

        # Boot 2: the transaction is gone but the cleanup post-condition is
        # not proven, so the device must not report a finalized rollback and
        # the staging tree stays the helper's to remove.
        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-rollback-resume-cleanup-incomplete", self.fx.markers())
        self.assert_untouched_failed_candidate()
        self.assertTrue((self.fx.update / "staging").exists())

        # Boot 3: once the cleanup is complete the same boot publishes.
        shutil.rmtree(self.fx.update / "staging")
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertFalse(self.fx.reboots.exists())

    def test_unpublished_check_record_is_recovered_from_a_terminal_state(self) -> None:
        # The state half was published before the interruption, so the pending
        # half is the check record the failed candidate left behind.
        self.fx.seed_finalized_rollback(terminal_state="rolled-back")
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_terminal_publication(self.fx.markers())

    def test_check_record_of_another_candidate_is_never_relabelled(self) -> None:
        self.fx.seed_finalized_rollback(terminal_state="rolled-back")
        self.fx.write_update(
            "check-status", "schema=1\nstatus=reboot-pending\nlatest_version=0.15.0\n"
        )
        before_state = (self.fx.update / "state").read_bytes()
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.fx.read_update("check-status")
        self.assertEqual(check["status"], "reboot-pending")
        self.assertEqual(check["latest_version"], "0.15.0")
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)

    def test_resume_fails_closed_without_the_rollback_history_record(self) -> None:
        self.fx.seed_failed_candidate()
        (self.fx.update / "state.tmp").mkdir()
        self.assertEqual(self.fx.boot().returncode, 0)
        (self.fx.update / "state.tmp").rmdir()
        # The only evidence that the rollback finished is gone, so the worker
        # must not claim a terminal state for it.
        (self.fx.update / "rolled-back").unlink()
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_untouched_failed_candidate()
        self.assertIn("ota-rollback-resume-evidence-invalid", self.fx.markers())

    def test_live_transaction_still_owns_its_publication(self) -> None:
        self.fx.seed_failed_candidate()
        # Boot with the transaction intact: the rollback branch publishes, and
        # the recovery path must not have pre-empted or duplicated it.
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.fx.assert_cleanup_happened()
        state = self.fx.read_update("state")
        self.assertEqual(state["state"], "rolled-back")
        self.assertIn("ota-v2-fallback-cleaned:", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )


if __name__ == "__main__":
    unittest.main()
