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
        retire_fn = extract_function(self.init, "ota_rollback_resume_live_records")
        self.assertIn("/data/libreecho/update/pending", retire_fn)
        self.assertIn("/data/libreecho/update/feature-commit", retire_fn)
        self.assertIn("ota_rollback_resume_live_records", resume)
        self.assertIn("s/^state=//p", resume)
        self.assertIn("detail=//p", resume)
        self.assertIn("s/^slot=//p", resume)
        self.assertIn('ota_rollback_publish_terminal "$resume_slot"', resume)
        # The helper retires the transaction before it removes its staging
        # tree, and it will not resume that cleanup once the transaction is
        # gone, so the resume finishes it under the same validation the helper
        # applies and publishes only afterwards.
        cleanup = extract_function(self.init, "ota_rollback_resume_staging_cleanup")
        self.assertIn("/data/libreecho/update/staging", cleanup)
        # The three shapes the helper's own validation rejects must be refused
        # here as well, so removal is never reached for an unvalidated path.
        self.assertIn('[ -L "$resume_staging" ]', cleanup)
        self.assertIn('[ ! -d "$resume_staging" ]', cleanup)
        self.assertIn("-type l", cleanup)
        self.assertIn("log ota-rollback-resume-staging-unsafe", cleanup)
        self.assertIn("log ota-rollback-resume-staging-cleanup-failed", cleanup)
        self.assertIn('$BB rm -rf "$resume_staging"', cleanup)
        # The helper retires the live transaction with one `rm -f` of the
        # pending record and the feature commit and then exits once either is
        # gone, so a one-sided retirement is resumed too -- but only for the
        # transaction the finalized history names, and only while its other
        # half is already gone: a complete pair is a live transaction whose own
        # rollback branch owns those records.
        retire = extract_function(self.init, "ota_rollback_resume_live_records")
        self.assertIn("/data/libreecho/update/pending", retire)
        self.assertIn("/data/libreecho/update/feature-commit", retire)
        self.assertIn("[ ! -e /data/libreecho/update/pending ] ||", retire)
        self.assertIn("transaction_id=//p", retire)
        self.assertIn(
            'log "ota-rollback-resume-live-record-foreign:$resume_live"', retire
        )
        self.assertIn(
            'log "ota-rollback-resume-live-record-unsafe:$resume_live"', retire
        )
        self.assertIn("ota-rollback-resume-live-record-cleaned:$resume_live", retire)
        self.assertLess(
            resume.index("ota_rollback_resume_live_records"),
            resume.index("ota_rollback_resume_staging_cleanup"),
        )
        self.assertLess(
            resume.index("ota_rollback_resume_staging_cleanup"),
            resume.index('ota_rollback_publish_terminal "$resume_slot"'),
        )
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

    def test_legacy_schema_one_rollback_is_resumed_without_the_v2_cleanups(self) -> None:
        # A schema-1 rollback retires its transaction by moving the pending
        # record itself, so the record an interrupted finalization leaves behind
        # carries no transaction id.  The resume must still publish for it --
        # otherwise its check record is stranded at `reboot-pending` forever --
        # and must not run the v2 cleanups, which exist for the helper's own
        # retirement steps and have no matching evidence on a legacy device.
        resume = extract_function(self.init, "ota_rollback_resume_terminal")
        self.assertIn("s/^transaction_id=//p", resume)
        self.assertIn("s/^schema=//p", resume)
        self.assertIn('if [ -n "$resume_transaction" ]; then', resume)
        self.assertIn('[ "$resume_schema" = 1 ] || {', resume)
        self.assertIn("ota-rollback-resume-legacy-publication", resume)
        self.assertIn(
            'log "ota-rollback-resume-live-record-present:$resume_live"', resume
        )
        # Both v2 cleanups sit inside the branch that only a record naming a
        # transaction reaches, and the single publisher follows both branches.
        self.assertLess(
            resume.index('if [ -n "$resume_transaction" ]; then'),
            resume.index("ota_rollback_resume_lock || return 0"),
        )
        self.assertLess(
            resume.index("ota_rollback_resume_lock || return 0"),
            resume.index("ota_rollback_resume_live_records"),
        )
        self.assertLess(
            resume.index("ota_rollback_resume_staging_cleanup"),
            resume.index("ota-rollback-resume-legacy-publication"),
        )
        self.assertLess(
            resume.index("ota-rollback-resume-legacy-publication"),
            resume.index('ota_rollback_publish_terminal "$resume_slot"'),
        )

    def test_resume_recognizes_the_failed_candidates_progress_records(self) -> None:
        # A candidate can exhaust its boot attempts -- or lose power -- before
        # the worker reaches its health failure record, so the last progress
        # record it leaves is the installer's `reboot-pending` or the worker's
        # own `boot-validating`.  Both have to be recognized, or the stranded
        # record is simply a different one.
        resume = extract_function(self.init, "ota_rollback_resume_terminal")
        self.assertIn("reboot-pending|boot-validating)", resume)
        self.assertIn("s/^state=//p", resume)
        # The evidence for a rollback is the finalized history record, and a
        # rollback leaves the previously confirmed slot running: a record naming
        # the slot this boot runs is retained history, not a publication.
        self.assertIn("ota-rollback-resume-history-slot-still-selected", resume)
        self.assertIn('[ "$resume_slot" != "$selected_slot" ]', resume)
        self.assertLess(
            resume.index('case "$resume_slot" in'),
            resume.index('[ "$resume_slot" != "$selected_slot" ]'),
        )
        self.assertLess(
            resume.index('[ "$resume_slot" != "$selected_slot" ]'),
            resume.index("ota_rollback_resume_live_records"),
        )
        # The slot the history record is checked against is the running one, so
        # the resume runs after this boot has determined it -- and still before
        # the boot decides whether it has a live transaction at all.
        self.assertLess(
            self.worker.index('libreecho-bootctl status "$running_slot"'),
            self.worker.index("\n    ota_rollback_resume_terminal\n"),
        )

    def test_resume_validates_the_history_record_before_it_reads_it(self) -> None:
        # The history record is the only authorization for the resumed removals,
        # and the helper refuses to read one that is not a bounded regular
        # non-symlink.  The resume applies the same shape test before it reads a
        # slot or a transaction id out of the record, and again once it holds
        # the install lock, so a path that is a symlink -- for example one
        # pointing at the surviving live record -- never becomes cleanup
        # authorization.
        resume = extract_function(self.init, "ota_rollback_resume_terminal")
        bounded = extract_function(self.init, "ota_rollback_resume_history_bounded")
        self.assertEqual(resume.count("ota_rollback_resume_history_bounded"), 2)
        self.assertIn("ota-rollback-resume-history-unsafe", resume)
        self.assertIn("ota-rollback-resume-evidence-invalid", resume)
        for probe in (
            "stat -c %s",
            "-le 8192",
            '[ -f "$resume_history" ]',
            '[ ! -L "$resume_history" ]',
        ):
            self.assertIn(probe, bounded)
        # Nothing is read out of the record before it passed the shape test,
        # and every read uses the validated path.
        first_check = resume.index("ota_rollback_resume_history_bounded; then")
        self.assertLess(first_check, resume.index("s/^slot=//p"))
        self.assertLess(first_check, resume.index("s/^transaction_id=//p"))
        self.assertIn("'s/^slot=//p' \"$resume_history\"", resume)
        self.assertNotIn("'s/^slot=//p' /data/libreecho/update/rolled-back", resume)
        # The second application is under the install lock, ahead of both
        # removals, so a record that changed while this boot was deciding is
        # refused as well.
        lock_at = resume.index("ota_rollback_resume_lock || return 0")
        self.assertLess(first_check, lock_at)
        self.assertLess(lock_at, resume.rindex("ota_rollback_resume_history_bounded; then"))
        self.assertLess(
            resume.rindex("ota_rollback_resume_history_bounded; then"),
            resume.index("ota_rollback_resume_live_records"),
        )

    def test_resumed_cleanup_is_serialized_with_the_update_flow(self) -> None:
        # Both resumed removals act inside the update root a concurrent
        # installation stages into, so they run under the same install lock
        # `libreecho-update` and `libreecho-update-fetch` take, and the lock is
        # released again on every path -- including each refusal.
        resume = extract_function(self.init, "ota_rollback_resume_terminal")
        lock = extract_function(self.init, "ota_rollback_resume_lock")
        unlock = extract_function(self.init, "ota_rollback_resume_unlock")
        self.assertIn("$BB mkdir /data/libreecho/update/install.lock", lock)
        self.assertIn("ota-rollback-resume-install-locked", lock)
        self.assertIn("$BB rmdir /data/libreecho/update/install.lock", unlock)
        self.assertEqual(resume.count("ota_rollback_resume_lock || return 0"), 1)
        # The two refusals that happen once the lock is held, and the completed
        # path, each release it.
        self.assertEqual(resume.count("ota_rollback_resume_unlock"), 3)
        lock_at = resume.index("ota_rollback_resume_lock || return 0")
        self.assertLess(lock_at, resume.index("ota_rollback_resume_live_records"))
        self.assertLess(lock_at, resume.index("ota_rollback_resume_staging_cleanup"))
        self.assertLess(
            resume.index("ota_rollback_resume_staging_cleanup"),
            resume.rindex("ota_rollback_resume_unlock"),
        )
        # A boot that cannot take the lock publishes nothing: the records stay
        # unpublished for the next boot to retry.
        self.assertLess(
            lock_at, resume.index('ota_rollback_publish_terminal "$resume_slot"')
        )
        # One lock covers both removals; the cleanup must not take a second one
        # of its own, which the helper's non-reentrant lock protocol forbids.
        cleanup = extract_function(self.init, "ota_rollback_resume_staging_cleanup")
        self.assertNotIn("install.lock", cleanup)
        self.assertNotIn("ota_rollback_resume_unlock", cleanup)

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
        # A schema-2 pending record without its durable journal was never
        # prepared or activated, so it is not rollback evidence: it must be
        # preserved for the update flow that rebuilds it instead of being
        # retired as a rollback whose staged tree is still in place.
        self.assertLess(
            self.worker.index("[ -r /data/libreecho/update/feature-commit ]"),
            self.worker.index('log "ota-v2-fallback-cleaned:'),
        )


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
            "    # A concurrent installation is free to stage into this update root\n"
            "    # as soon as the live records are gone, so a boot that resumed the\n"
            "    # interrupted cleanup can find a different transaction -- or a\n"
            "    # replaced history record -- under the install lock it takes.  That\n"
            "    # boundary is modelled by acting at the moment of the lock.\n"
            "    mkdir)\n"
            '        if [ "${2##*/}" = install.lock ]; then\n'
            '            case "${PLANT_UNDER_LOCK:-}" in\n'
            "                live-transaction)\n"
            '                    printf \'schema=2\\ntransaction_id=cafebabe\\nversion=9.9.9\\nslot=b\\n\' >"${2%/*}/pending"\n'
            '                    printf \'format=libreecho-ota-v2\\nversion=9.9.9\\n\' >"${2%/*}/staging/manifest"\n'
            "                    ;;\n"
            "                unsafe-history)\n"
            f'                    "{BUSYBOX}" rm -f "${{2%/*}}/rolled-back"\n'
            f'                    "{BUSYBOX}" ln -s "${{2%/*}}/feature-commit" "${{2%/*}}/rolled-back"\n'
            "                    ;;\n"
            "            esac\n"
            "        fi\n"
            "        ;;\n"
            "    mv)\n"
            f'        "{BUSYBOX}" "$@"\n'
            "        rc=$?\n"
            "        # A power loss placed on the rename the terminal publication\n"
            "        # performs between its two records: the state record reaches\n"
            "        # disk and the check record never does.\n"
            '        if [ "${INTERRUPT_AFTER_STATE_RENAME:-0}" = 1 ] &&\n'
            '           [ "${3##*/}" = state ]; then\n'
            '            kill -KILL "$PPID"\n'
            "        fi\n"
            '        exit "$rc"\n'
            "        ;;\n"
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
            # The shipped helper retires the pair with one `rm -f` of the
            # pending record and the feature commit -- in that order -- and
            # only then removes its staging tree, so a power loss can be placed
            # on either side of each step.
            'rm -f "$update/pending"\n'
            'if [ "${INTERRUPT_AFTER_PENDING_UNLINK:-0}" = 1 ]; then\n'
            '    kill -KILL "$PPID"\n'
            "    exit 0\n"
            "fi\n"
            'rm -f "$update/feature-commit"\n'
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

    def seed_failed_candidate(self, state_record: str | None = None) -> None:
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
            state_record
            or "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:web-status\n",
        )
        self.write_update(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=reboot-pending\nsource_reachable=true\n"
            f"latest_version={ROLLBACK_VERSION}\nlast_check_epoch=1\n",
        )

    def seed_legacy_failed_candidate(self, latest_version: str = ROLLBACK_VERSION) -> None:
        """A schema-1 (v1 updater) rollback boundary: the candidate staged on
        slot ``b``, the health-confirm failure record the worker wrote before
        it rebooted, and the check record the failed candidate left.

        The v1 updater's pending record *is* the rollback evidence -- the boot
        worker retires that transaction by moving the record itself -- so it
        carries no transaction id, and the durable v2 journal the schema-2
        rollback has does not exist for it.
        """
        self.write_update(
            "pending",
            f"schema=1\nversion={ROLLBACK_VERSION}\nslot={ROLLBACK_SLOT}\n"
            f"boot_sha256={'0' * 64}\nupdate_channel=dev\nfeature_policy=preserve\n",
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
            f"latest_version={latest_version}\nlast_check_epoch=1\n",
        )

    def seed_legacy_rollback_record(self, latest_version: str = ROLLBACK_VERSION) -> None:
        """The legacy device once the moved pending record is the rollback
        history and the check record is still the failed candidate's."""
        self.write_update(
            "rolled-back",
            f"schema=1\nversion={ROLLBACK_VERSION}\nslot={ROLLBACK_SLOT}\n"
            f"boot_sha256={'0' * 64}\nupdate_channel=dev\nfeature_policy=preserve\n",
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
            f"latest_version={latest_version}\nlast_check_epoch=1\n",
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

    def boot(
        self,
        interrupt_at: str | None = None,
        plant_under_lock: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one boot of the shipped worker against the sandbox root.

        ``interrupt_at`` places the power loss inside the recovery helper:
        "pending-unlink" between the two unlinks of its live-record cleanup,
        "staging" after that cleanup but before its staging tree is removed,
        "cleanup" once both are done, "state" between the two renames of the
        terminal publication, and ``None`` for a boot that completes.

        ``plant_under_lock`` models a concurrent installation that acts at the
        moment the resumed cleanup takes the shared install lock: a complete
        replacement transaction with its own staged tree
        (``"live-transaction"``), or a history record replaced by a symlink to
        the live record (``"unsafe-history"``).
        """
        return subprocess.run(
            [BUSYBOX, "sh", str(self.harness)],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "BOOT_LOG": str(self.boot_log),
                "PLANT_UNDER_LOCK": plant_under_lock or "",
                "INTERRUPT_AFTER_PENDING_UNLINK": (
                    "1" if interrupt_at == "pending-unlink" else "0"
                ),
                "INTERRUPT_BEFORE_STAGING_CLEANUP": (
                    "1" if interrupt_at == "staging" else "0"
                ),
                "INTERRUPT_AFTER_CLEANUP": "1" if interrupt_at == "cleanup" else "0",
                "INTERRUPT_AFTER_STATE_RENAME": "1" if interrupt_at == "state" else "0",
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

    def assert_terminal_publication(self, markers: str, resumed: bool = True) -> None:
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
        if resumed:
            self.assertIn(
                f"ota-rollback-terminal-publication-resumed:{ROLLBACK_SLOT}", markers
            )
        else:
            # The rollback branch itself published: the recovery path must not
            # have pre-empted or duplicated it.
            self.assertNotIn("ota-rollback-terminal-publication-resumed", markers)

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

    def assert_legacy_rollback_evidence(self) -> None:
        """The schema-1 rollback record is the retired pending record itself."""
        record = self.fx.read_update("rolled-back")
        self.assertEqual(record["schema"], "1")
        self.assertNotIn("transaction_id", record)
        self.assertEqual(record["version"], ROLLBACK_VERSION)
        self.assertEqual(record["slot"], ROLLBACK_SLOT)

    def assert_no_v2_cleanup(self) -> None:
        markers = self.fx.markers()
        for v2_marker in (
            "ota-rollback-resume-live-record-cleaned",
            "ota-rollback-resume-live-record-foreign",
            "ota-rollback-resume-live-record-unsafe",
            "ota-rollback-resume-staging-cleaned",
            "ota-rollback-resume-staging-unsafe",
            "ota-rollback-resume-staging-cleanup-failed",
        ):
            self.assertNotIn(v2_marker, markers)
        # The recovery helper itself is never invoked for a legacy record.
        self.assertFalse(self.fx.fallbacks.exists())

    def plant_legacy_staging(self) -> Path:
        """The v1 updater's staged tree, which no schema-1 path removes."""
        staging = self.fx.update / "staging"
        staging.mkdir()
        manifest = staging / "manifest"
        manifest.write_text("format=libreecho-ota-v1\n")
        return manifest

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

    def test_interrupted_staging_cleanup_is_finished_by_the_next_boot(self) -> None:
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

        # Boot 2: the helper exits immediately once the transaction is gone, so
        # no production path would finish that cleanup -- the worker has to do
        # it, and it publishes the terminal records only afterwards.  No
        # operator action and no second rollback are involved.
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assertFalse((self.fx.update / "staging").exists())
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        self.assertFalse(self.fx.reboots.exists())

    def test_staging_tree_that_cannot_be_validated_is_left_for_recovery(self) -> None:
        self.fx.seed_failed_candidate()
        self.assertEqual(
            self.fx.boot(interrupt_at="staging").returncode, -signal.SIGKILL
        )
        self.assertTrue((self.fx.update / "staging").is_dir())
        # A symlink in the tree is exactly what the helper's own validation
        # refuses, so the worker must not force the removal either.
        unsafe = self.fx.update / "staging" / "manifest"
        unsafe.symlink_to("/etc/passwd")

        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-rollback-resume-staging-unsafe", self.fx.markers())
        self.assert_untouched_failed_candidate()
        self.assertTrue(unsafe.is_symlink())

        # Boot 3: once the unsafe entry is gone, the same boot finishes the
        # cleanup and publishes.
        unsafe.unlink()
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse((self.fx.update / "staging").exists())
        self.assert_terminal_publication(self.fx.markers())

    def test_unvalidated_staging_shape_is_left_for_recovery(self) -> None:
        # The removal the resume performs is only ever reached for a staging
        # tree the helper would itself accept.  Every shape that validation
        # rejects -- a symlinked tree, a tree that is not a directory, and a
        # dangling symlink -- must be left exactly as found, with the terminal
        # records still unpublished, and the same boot must finish the cleanup
        # and publish once the shape is gone.  Otherwise a refusal would be
        # indistinguishable from the stranding this path exists to fix.
        for shape in ("symlink-tree", "not-a-directory", "dangling-symlink"):
            with self.subTest(shape=shape):
                tmp = Path(tempfile.mkdtemp(prefix="libreecho-boot-worker-"))
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                fx = _BootWorkerFixture(tmp)
                fx.seed_failed_candidate()
                self.assertEqual(
                    fx.boot(interrupt_at="staging").returncode, -signal.SIGKILL
                )
                staging = fx.update / "staging"
                self.assertTrue(staging.is_dir())
                shutil.rmtree(staging)
                outside = tmp / "outside"
                if shape == "symlink-tree":
                    outside.mkdir()
                    (outside / "keep").write_text("keep\n")
                    staging.symlink_to(outside)
                elif shape == "not-a-directory":
                    staging.write_text("not-a-tree\n")
                else:
                    staging.symlink_to(outside)

                refused = fx.boot()
                self.assertEqual(refused.returncode, 0, refused.stderr)
                self.assertIn("ota-rollback-resume-staging-unsafe", fx.markers())
                self.assertNotIn(
                    "ota-rollback-terminal-publication-resumed", fx.markers()
                )
                self.assertEqual(fx.read_update("state")["state"], "restarting")
                self.assertEqual(
                    fx.read_update("check-status")["status"], "reboot-pending"
                )
                # Refusing means leaving it alone, not deleting it by another
                # route.
                if shape == "not-a-directory":
                    self.assertEqual(staging.read_text(), "not-a-tree\n")
                else:
                    self.assertTrue(staging.is_symlink())
                    self.assertEqual(os.readlink(staging), str(outside))
                if shape == "symlink-tree":
                    self.assertEqual((outside / "keep").read_text(), "keep\n")

                staging.unlink()
                recovered = fx.boot()
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertFalse((fx.update / "staging").exists())
                self.assertIn(
                    "ota-rollback-terminal-publication-resumed:b", fx.markers()
                )

    def test_symlinked_rollback_history_never_authorizes_the_cleanup(self) -> None:
        # The history record is the only thing that authorizes the resumed
        # removals, and the helper refuses to read one that is not a bounded
        # regular non-symlink.  A symlink to the surviving live record -- which
        # names both a slot this boot is not running and the transaction id the
        # resume would match -- must be refused the same way, or a path that is
        # not rollback evidence at all is used to delete the records it points
        # at.
        self.fx.write_update(
            "feature-commit",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        self.fx.write_update(
            "state",
            "schema=1\nstate=restarting\nprogress=0\n"
            "detail=health-confirm-failed:web-status\n",
        )
        self.fx.write_update(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=reboot-pending\nsource_reachable=true\n"
            f"latest_version={ROLLBACK_VERSION}\nlast_check_epoch=1\n",
        )
        (self.fx.update / "staging").mkdir()
        (self.fx.update / "staging" / "manifest").write_text(
            "format=libreecho-ota-v2\n"
        )
        history = self.fx.update / "rolled-back"
        history.symlink_to(self.fx.update / "feature-commit")
        before_state = (self.fx.update / "state").read_bytes()
        before_check = (self.fx.update / "check-status").read_bytes()
        before_journal = (self.fx.update / "feature-commit").read_bytes()

        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-rollback-resume-history-unsafe", self.fx.markers())
        # Nothing is read through the symlink, and neither the record it points
        # at nor the staged tree is removed.
        self.assertNotIn("ota-rollback-resume-live-record-cleaned", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        self.assertTrue(history.is_symlink())
        self.assertEqual(
            (self.fx.update / "feature-commit").read_bytes(), before_journal
        )
        self.assertTrue((self.fx.update / "staging").is_dir())
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), before_check)

        # Once the record is the regular file the helper itself would accept,
        # the interruption is finished instead of being stranded by it.
        history.unlink()
        self.fx.write_update(
            "feature-commit", "phase=prepared\ntransaction_id=deadbeef\n"
        )
        self.fx.write_update(
            "rolled-back",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn(
            "ota-rollback-resume-live-record-cleaned:feature-commit",
            self.fx.markers(),
        )
        self.assertIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assert_terminal_publication(self.fx.markers())
        self.assertFalse(self.fx.reboots.exists())

    def test_installer_holding_the_install_lock_blocks_the_resumed_cleanup(self) -> None:
        self.fx.seed_failed_candidate()
        self.assertEqual(
            self.fx.boot(interrupt_at="staging").returncode, -signal.SIGKILL
        )
        self.assertTrue((self.fx.update / "staging").is_dir())
        before_state = (self.fx.update / "state").read_bytes()
        before_check = (self.fx.update / "check-status").read_bytes()
        # The lock is the one `libreecho-update` and `libreecho-update-fetch`
        # take before they stage into this tree, so a boot that finds it held is
        # looking at an installation in flight, which owns the tree until it
        # finishes: leave it and the records alone and let a later boot retry.
        lock = self.fx.update / "install.lock"
        lock.mkdir()

        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-rollback-resume-install-locked", self.fx.markers())
        self.assertNotIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        self.assertTrue((self.fx.update / "staging").is_dir())
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), before_check)
        # The in-flight installation still holds the lock: this worker released
        # nothing it did not take.
        self.assertTrue(lock.is_dir())

        lock.rmdir()
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assert_terminal_publication(self.fx.markers())
        self.assertFalse(lock.exists())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        self.assertFalse(self.fx.reboots.exists())

    def test_staging_replaced_under_the_lock_is_never_removed(self) -> None:
        self.fx.seed_failed_candidate()
        self.assertEqual(
            self.fx.boot(interrupt_at="staging").returncode, -signal.SIGKILL
        )
        self.assertTrue((self.fx.update / "staging").is_dir())
        before_state = (self.fx.update / "state").read_bytes()
        before_check = (self.fx.update / "check-status").read_bytes()

        # The replacement lands while this boot is between its decision to
        # clean up and the install lock it takes to do it: a new candidate's
        # intent record for the other slot plus its staged tree, which is what
        # the update flow writes into this same root.  The lock is what makes
        # that impossible in production; the boot also revalidates under the
        # lock what it is about to remove and refuses.
        refused = self.fx.boot(plant_under_lock="live-transaction")
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn(
            "ota-rollback-resume-live-record-foreign:pending", self.fx.markers()
        )
        self.assertNotIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        # The new candidate's record and staged tree are untouched, and the
        # rollback history published nothing.
        self.assertEqual(self.fx.read_update("pending")["transaction_id"], "cafebabe")
        self.assertEqual(self.fx.read_update("pending")["version"], "9.9.9")
        self.assertFalse((self.fx.update / "feature-commit").exists())
        self.assertEqual(
            (self.fx.update / "staging" / "manifest").read_text(),
            "format=libreecho-ota-v2\nversion=9.9.9\n",
        )
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), before_check)
        # The boot released the lock it took, and no second rollback ran.
        self.assertFalse((self.fx.update / "install.lock").exists())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])

        # That record is its own recovery path; once it is gone the interrupted
        # rollback cleanup is finished and published, and the replacement tree
        # -- no longer owned by any transaction -- goes with it.
        (self.fx.update / "pending").unlink()
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assert_terminal_publication(self.fx.markers())

    def test_history_replaced_under_the_lock_is_never_used(self) -> None:
        self.fx.seed_failed_candidate()
        self.assertEqual(
            self.fx.boot(interrupt_at="staging").returncode, -signal.SIGKILL
        )
        before_state = (self.fx.update / "state").read_bytes()
        before_check = (self.fx.update / "check-status").read_bytes()

        # The record is validated again after the lock is taken, so a history
        # that stopped being a bounded regular non-symlink while this boot was
        # deciding is not acted on either.
        refused = self.fx.boot(plant_under_lock="unsafe-history")
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-rollback-resume-history-unsafe", self.fx.markers())
        self.assertNotIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assertNotIn("ota-rollback-resume-live-record-cleaned", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        self.assertTrue((self.fx.update / "rolled-back").is_symlink())
        self.assertTrue((self.fx.update / "staging").is_dir())
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), before_check)
        # The lock the boot took is released even on that refusal.
        self.assertFalse((self.fx.update / "install.lock").exists())

        (self.fx.update / "rolled-back").unlink()
        self.fx.write_update(
            "rolled-back",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("ota-rollback-resume-staging-cleaned", self.fx.markers())
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])

    def test_one_sided_retirement_is_finished_by_the_next_boot(self) -> None:
        self.fx.seed_failed_candidate()
        # Boot 1: the helper records the fallback history and retires the live
        # transaction, and the device loses power between the two unlinks of
        # its single `rm -f`, so the durable journal survives without the
        # pending record.
        interrupted = self.fx.boot(interrupt_at="pending-unlink")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.assertFalse((self.fx.update / "pending").exists())
        self.assertTrue((self.fx.update / "feature-commit").is_file())
        self.assertTrue((self.fx.update / "staging").is_dir())
        self.assertTrue((self.fx.update / "rolled-back").is_file())
        self.assert_untouched_failed_candidate()

        # Boot 2: neither the helper nor `abort-before-activation` will touch a
        # one-sided pair, so no other production path retires that journal --
        # and while it exists the update flow refuses to stage any candidate.
        # The worker retires the record the finalized history names, finishes
        # the staging cleanup, and only then publishes.
        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn(
            "ota-rollback-resume-live-record-cleaned:feature-commit",
            self.fx.markers(),
        )
        self.assertFalse((self.fx.update / "feature-commit").exists())
        self.assertFalse((self.fx.update / "staging").exists())
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        self.assertFalse(self.fx.reboots.exists())

    def test_live_records_of_another_transaction_are_never_retired(self) -> None:
        self.fx.seed_failed_candidate()
        self.assertEqual(
            self.fx.boot(interrupt_at="pending-unlink").returncode, -signal.SIGKILL
        )
        # A journal that does not belong to the transaction the finalized
        # history names is somebody else's live transaction: leave it.
        self.fx.write_update(
            "feature-commit", "phase=prepared\ntransaction_id=cafebabe\n"
        )
        foreign = self.fx.boot()
        self.assertEqual(foreign.returncode, 0, foreign.stderr)
        self.assertIn(
            "ota-rollback-resume-live-record-foreign:feature-commit",
            self.fx.markers(),
        )
        self.assertTrue((self.fx.update / "feature-commit").is_file())
        self.assert_untouched_failed_candidate()
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )

        # A complete pair is a live transaction even when its id matches the
        # history record, so the resume must not retire it: the rollback branch
        # owns those records.
        self.fx.write_update(
            "pending",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        self.fx.write_update(
            "feature-commit", "phase=prepared\ntransaction_id=deadbeef\n"
        )
        live = self.fx.boot()
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertNotIn("ota-rollback-resume-live-record-cleaned", self.fx.markers())
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        self.assertIn(f"ota-v2-fallback-cleaned:{ROLLBACK_SLOT}:a", self.fx.markers())

    def test_schema_two_intent_without_its_journal_is_preserved(self) -> None:
        # A lone schema-2 pending record is an interrupted intent publish, not
        # rollback evidence: retiring it as a rollback would publish a
        # finalization that never happened while its staged tree still holds
        # the candidate.  It is preserved for the update flow that rebuilds it.
        self.fx.write_update(
            "pending",
            f"schema=2\ntransaction_id=deadbeef\nversion={ROLLBACK_VERSION}\n"
            f"slot={ROLLBACK_SLOT}\n",
        )
        self.fx.write_update(
            "state",
            f"schema=1\nstate=downloading\nprogress=10\ndetail={ROLLBACK_VERSION}\n",
        )
        self.fx.write_update(
            "check-status",
            "schema=1\nsource=github-releases\nchannel=dev\n"
            "status=reboot-pending\nsource_reachable=true\n"
            f"latest_version={ROLLBACK_VERSION}\nlast_check_epoch=1\n",
        )
        (self.fx.update / "staging").mkdir()
        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("ota-v2-fallback-preserved-for-recovery", self.fx.markers())
        self.assertTrue((self.fx.update / "pending").is_file())
        self.assertFalse((self.fx.update / "rolled-back").exists())
        self.assertEqual(self.fx.read_update("state")["state"], "downloading")
        self.assertTrue((self.fx.update / "staging").is_dir())

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

    def test_legacy_schema_one_rollback_is_finalized_by_the_same_boot(self) -> None:
        # The un-interrupted boundary this path exists for: a schema-1 rollback
        # retires its transaction by moving the pending record itself -- there
        # is no journal to retire and no transaction id in the record a later
        # boot reads -- and then publishes the same terminal status.
        self.fx.seed_legacy_failed_candidate()
        manifest = self.plant_legacy_staging()
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fx.update / "pending").exists())
        self.assert_legacy_rollback_evidence()
        self.assert_terminal_publication(self.fx.markers(), resumed=False)
        self.assertIn(
            f"ota-rollback-complete:{ROLLBACK_SLOT}:a", self.fx.markers()
        )
        # The v1 updater's staged tree is left as the schema-1 branch leaves it.
        self.assertEqual(manifest.read_text(), "format=libreecho-ota-v1\n")
        self.assertFalse(self.fx.reboots.exists())

    def test_interrupted_legacy_check_publication_is_finished_by_the_next_boot(self) -> None:
        # The boundary the schema-1 record is stranded at: the pending record is
        # moved, the state half of the publication reaches disk, and the power
        # is lost before the check record is renamed.  The next boot has no live
        # transaction to enter the rollback branch with and no transaction id to
        # match, so the legacy record it finds is the only evidence that the
        # rollback finished -- without the legacy branch of the resume the check
        # record would stay `reboot-pending` on every boot.
        self.fx.seed_legacy_failed_candidate()
        manifest = self.plant_legacy_staging()
        interrupted = self.fx.boot(interrupt_at="state")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.assertFalse((self.fx.update / "pending").exists())
        self.assert_legacy_rollback_evidence()
        state = self.fx.read_update("state")
        self.assertEqual(state["state"], "rolled-back")
        self.assertEqual(state["detail"], ROLLBACK_SLOT)
        self.assertEqual(
            self.fx.read_update("check-status")["status"], "reboot-pending"
        )

        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertIn("ota-rollback-resume-legacy-publication", self.fx.markers())
        # The legacy resume publishes and does nothing else: neither v2 cleanup
        # belongs to a record with no journal and no v2 staged tree, the v1
        # staged tree is left exactly as found, and no second rollback runs.
        self.assert_no_v2_cleanup()
        self.assertEqual(manifest.read_text(), "format=libreecho-ota-v1\n")
        self.assertEqual(
            self.fx.markers().count("ota-rollback-terminal-publication-resumed"), 1
        )
        self.assertFalse(self.fx.reboots.exists())

        # A later boot is a no-op: the records are already terminal.
        before_state = (self.fx.update / "state").read_bytes()
        terminal = (self.fx.update / "check-status").read_bytes()
        third = self.fx.boot()
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual((self.fx.update / "check-status").read_bytes(), terminal)
        self.assertFalse(self.fx.reboots.exists())

    def test_legacy_publication_that_never_wrote_a_record_is_finished_by_the_next_boot(self) -> None:
        # The schema-1 branch publishes without checking the writer's status, so
        # a state write that fails -- or a power loss before it -- leaves the
        # moved record as the only live evidence.  Both halves are finished by
        # the next boot.
        self.fx.seed_legacy_failed_candidate()
        (self.fx.update / "state.tmp").mkdir()
        refused = self.fx.boot()
        self.assertEqual(refused.returncode, 0, refused.stderr)
        (self.fx.update / "state.tmp").rmdir()
        self.assertFalse((self.fx.update / "pending").exists())
        self.assert_legacy_rollback_evidence()
        self.assert_untouched_failed_candidate()

        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assert_no_v2_cleanup()
        self.assertFalse(self.fx.reboots.exists())

    def test_legacy_resume_never_relabels_an_unrelated_check_record(self) -> None:
        # A legacy record carries no transaction id, so the version the check
        # record names is the only thing tying the publication to the failed
        # candidate: a check record for another candidate is not this
        # rollback's to rewrite.
        self.fx.seed_legacy_rollback_record(latest_version="0.15.0")
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.fx.read_update("state")
        self.assertEqual(state["state"], "rolled-back")
        self.assertEqual(state["detail"], ROLLBACK_SLOT)
        check = self.fx.read_update("check-status")
        self.assertEqual(check["status"], "reboot-pending")
        self.assertEqual(check["latest_version"], "0.15.0")
        self.assertFalse((self.fx.update / "check-status.tmp").exists())

    def test_interrupted_publication_is_finished_from_the_installer_state(self) -> None:
        # A candidate that crashes or loses power on its boots can exhaust its
        # attempts before this worker ever writes its restart record, so the
        # last progress record it left is the installer's `reboot-pending`.  The
        # fallback boot then retires the transaction and can lose power before
        # the publication, and the resume has to recognize that progress record
        # as the failed candidate's: the finalized history record is what proves
        # the transaction was retired.
        self.fx.seed_failed_candidate(
            state_record="schema=1\nstate=reboot-pending\nprogress=100\ndetail=b\n"
        )
        interrupted = self.fx.boot(interrupt_at="cleanup")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.fx.assert_cleanup_happened()
        self.assertEqual(self.fx.read_update("state")["state"], "reboot-pending")

        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        self.assertFalse(self.fx.reboots.exists())

    def test_interrupted_publication_is_finished_from_the_pre_check_state(self) -> None:
        # The other progress record the same failure can leave behind: the
        # `boot-validating` state this worker writes before its health checks, so
        # the candidate died without ever reaching its restart record.
        self.fx.seed_failed_candidate(
            state_record="schema=1\nstate=boot-validating\nprogress=95\n"
            "detail=health-checks\n"
        )
        interrupted = self.fx.boot(interrupt_at="cleanup")
        self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stderr)
        self.fx.assert_cleanup_happened()
        self.assertEqual(self.fx.read_update("state")["state"], "boot-validating")

        recovered = self.fx.boot()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertEqual(self.fx.fallbacks.read_text().splitlines(), ["fallback"])
        self.assertFalse(self.fx.reboots.exists())

    def test_interrupted_legacy_publication_is_finished_from_the_installer_state(self) -> None:
        # The same pre-health-check state on a legacy device: the moved pending
        # record is the rollback evidence, the installer's progress record
        # survives, and neither v2 cleanup belongs to it.
        self.fx.seed_legacy_rollback_record()
        self.fx.write_update(
            "state", "schema=1\nstate=reboot-pending\nprogress=100\ndetail=b\n"
        )
        manifest = self.plant_legacy_staging()
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_terminal_publication(self.fx.markers())
        self.assertIn("ota-rollback-resume-legacy-publication", self.fx.markers())
        self.assert_no_v2_cleanup()
        self.assertEqual(manifest.read_text(), "format=libreecho-ota-v1\n")

    def test_history_naming_the_running_slot_is_never_published(self) -> None:
        # A retained history record outlives the rollback it describes: the
        # device can go on to install and confirm a later candidate on that same
        # slot, and the record then names the slot the device is running.  That
        # is not a rollback waiting for a publication -- the rollback branch
        # itself only retires a transaction staged on a slot other than the one
        # that booted -- so neither record may be rewritten from it.
        self.fx.seed_legacy_rollback_record()
        self.fx.write_update(
            "rolled-back",
            f"schema=1\nversion={ROLLBACK_VERSION}\nslot=a\n"
            f"boot_sha256={'0' * 64}\nupdate_channel=dev\nfeature_policy=preserve\n",
        )
        self.fx.write_update(
            "state", "schema=1\nstate=reboot-pending\nprogress=100\ndetail=a\n"
        )
        before_state = (self.fx.update / "state").read_bytes()
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ota-rollback-resume-history-slot-still-selected", self.fx.markers()
        )
        self.assertNotIn(
            "ota-rollback-terminal-publication-resumed", self.fx.markers()
        )
        self.assertEqual((self.fx.update / "state").read_bytes(), before_state)
        self.assertEqual(
            self.fx.read_update("check-status")["status"], "reboot-pending"
        )

    def test_legacy_resume_stands_down_while_a_live_record_exists(self) -> None:
        # A schema-1 record cannot be matched to a surviving live record -- it
        # names no transaction -- so a device that still has one keeps that
        # record for its own recovery path instead of publishing a finalization
        # on evidence that cannot be attributed to it.
        self.fx.seed_legacy_rollback_record()
        self.fx.write_update(
            "feature-commit", "phase=prepared\ntransaction_id=cafebabe\n"
        )
        result = self.fx.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ota-rollback-resume-live-record-present:feature-commit",
            self.fx.markers(),
        )
        self.assertTrue((self.fx.update / "feature-commit").is_file())
        self.assert_untouched_failed_candidate()
        self.assertFalse(self.fx.fallbacks.exists())


if __name__ == "__main__":
    unittest.main()
