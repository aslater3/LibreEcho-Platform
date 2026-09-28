#!/usr/bin/env python3
"""Host-only incident recovery fixture; helper is a deterministic stand-in, not a signature test."""
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'operator-stale-lock-recovery.sh'
BB = '/usr/bin/busybox'
CANDIDATE_INIT = HERE / 'initramfs/libreecho-init'  # Recovery fragment matches the signed candidate byte-for-byte.


def digest(data):
    return hashlib.sha256(data).hexdigest()


class OperatorRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'data/libreecho/update'
        self.runroot = self.base / 'run/libreecho'
        self.proc = self.base / 'proc'
        self.root.mkdir(parents=True)
        self.runroot.mkdir(parents=True)
        self.proc.mkdir()
        (self.proc / 'stat').write_text('cpu 1 2 3\nbtime 2000000000\n')
        (self.proc / 'boot_id').write_text('boot-current\n')
        self.lock = self.root / 'install.lock'
        self.lock.mkdir()
        self.manifest = b'transaction_id=incident-014\n'
        self.pending = b'schema=2\ntransaction_id=incident-014\nversion=v014\nslot=a\n'
        (self.root / 'pending').write_bytes(self.pending)
        (self.root / 'feature-commit').write_text('transaction_id=incident-014\n')
        staging = self.root / 'staging'
        staging.mkdir()
        (staging / 'manifest').write_bytes(self.manifest)
        (staging / 'manifest.sig').write_text('synthetic fixture\n')
        (staging / 'bootctl.readback').write_text('selected_slot=a\nslot_a_success=1\nslot_b_success=0\n')
        self.bootctl = self.base / 'bootctl'
        self.bootctl.write_text('#!/bin/sh\nprintf "selected_slot=b\\nslot_a_success=0\\nslot_b_success=1\\n"\n')
        self.bootctl.chmod(0o700)
        self.running = self.proc / 'cmdline'
        self.running.write_text('root=/dev/ram androidboot.slot_suffix=_b\n')
        (self.root / 'check-status').write_text('status=reboot-pending\nlatest_version=v014\n')
        (self.root / 'state').write_text('schema=1\nstate=restarting\ndetail=health-confirm-failed:test\n')
        self.helper = self.base / 'signed-candidate-helper'
        self.helper.write_text('#!/bin/sh\ncase "$1" in\nrollback-evidence) printf "rollback-evidence transaction_id=incident-014 slot=b\\n";;\nfallback) cp "' + str(self.root / 'pending') + '" "' + str(self.root / 'rolled-back') + '"; rm -f "' + str(self.root / 'pending') + '" "' + str(self.root / 'feature-commit') + '"; rm -rf "' + str(staging) + '"; printf "fallback-cleaned transaction_id=incident-014\\n";;\n*) exit 1;;\nesac\n')
        self.helper.chmod(0o700)
        self.args = ['incident-014', digest(self.pending), digest(self.manifest), digest(self.helper.read_bytes()), str(self.helper)]
        self.script = self.base / 'operator.sh'
        self.script.write_text(SOURCE.read_text().replace('/data/libreecho/update', str(self.root)).replace('/run/libreecho', str(self.runroot)).replace('/proc/stat', str(self.proc / 'stat')).replace('/proc/sys/kernel/random/boot_id', str(self.proc / 'boot_id')).replace('/proc/cmdline', str(self.running)).replace('/usr/local/sbin/libreecho-bootctl', str(self.bootctl)).replace('f77cd968efe2fc9e3f5f9b70be99e4b82aabf56295820e4f1cf96a5507f98b34', self.args[3]).replace('/usr/local/sbin/libreecho-feature-transaction', str(self.helper)).replace('/bin/busybox', BB))
        self.script.chmod(0o700)
        self.old_lock()

    def old_lock(self):
        # Synthetic btime is in the future; real FS ctime is before it.
        self.assertLess(self.lock.stat().st_ctime, 2000000000)

    def run_recovery(self, args=None):
        return subprocess.run([BB, 'sh', str(self.script), *(args or self.args)], capture_output=True, text=True, timeout=15)

    def test_old_untagged_claim_and_authenticated_fallback_publish(self):
        r = self.run_recovery()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.root / 'pending').exists())
        self.assertIn('state=rolled-back', (self.root / 'state').read_text())
        self.assertIn('status=update-held-after-rollback', (self.root / 'check-status').read_text())
        self.assertIn('fallback-cleaned transaction_id=incident-014', r.stdout)

    def test_concurrent_claim_one_winner(self):
        p = [subprocess.Popen([BB, 'sh', str(self.script), *self.args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outcomes = [x.communicate(timeout=15) for x in p]
        self.assertEqual(sorted(x.returncode for x in p), [0, 1], outcomes)

    def test_current_boot_lock_refused(self):
        (self.proc / 'stat').write_text('btime 1\n')
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.assertFalse((self.lock / 'owner').exists())

    def test_foreign_owner_refused(self):
        (self.lock / 'owner').write_text('owner=rollback-resume\nboot_id=old\n')
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.lock / 'owner').exists())
        self.assertTrue((self.root / 'pending').exists())

    def test_fifo_owner_is_refused_without_blocking(self):
        os.mkfifo(self.lock / 'owner', 0o600)
        p = subprocess.Popen([BB, 'sh', str(self.script), *self.args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        try:
            try:
                _, stderr = p.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                self.fail('foreign FIFO owner blocked the recovery while holding fetch.lock')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('RECOVERY_REFUSED:foreign_owner', stderr)
            self.assertFalse((self.runroot / 'fetch.lock').exists())
            self.assertTrue((self.root / 'pending').exists())
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if p.poll() is None:
                p.communicate(timeout=5)

    def test_oversized_owner_is_refused(self):
        owner = self.lock / 'owner'
        owner.write_text('boot_id=old\n' + 'X' * 513)
        owner.chmod(0o600)
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('RECOVERY_REFUSED:foreign_owner', r.stderr)
        self.assertTrue((self.root / 'pending').exists())
        self.assertFalse((self.runroot / 'fetch.lock').exists())

    def test_changed_transaction_auth_refused(self):
        (self.root / 'staging/manifest').write_bytes(b'transaction_id=other\n')
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.assertTrue(self.lock.exists())

    def test_interrupted_cleanup_resumes_prior_boot_owned_tag(self):
        # Failed status publication leaves the owner tag across a modeled reboot.
        (self.root / 'check-status.tmp').mkdir()
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue((self.lock / 'owner').exists())
        self.assertEqual((self.lock / 'owner').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / 'pending').exists())
        (self.root / 'check-status.tmp').rmdir()
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.runroot / 'fetch.lock').exists(), 'recovery must release its fetch lock')
        self.assertIn('state=rolled-back', (self.root / 'state').read_text())

    def test_worker_fragment_comes_from_checkout_not_private_candidate_cache(self):
        self.assertEqual(CANDIDATE_INIT, HERE / 'initramfs/libreecho-init')
        self.assertTrue(CANDIDATE_INIT.is_file())

    def test_ci_runs_recovery_regression_and_validates_target_shell(self):
        workflow = (HERE.parent.parent / '.github/workflows/ota-release.yml').read_text()
        self.assertIn('            test_operator_stale_lock_recovery.py \\', workflow)
        self.assertIn('            tools/mt8163-arm32/test_operator_stale_lock_recovery.py \\', workflow)
        self.assertIn('          /bin/busybox sh -n tools/mt8163-arm32/operator-stale-lock-recovery.sh', workflow)

    def test_crash_after_readback_write_does_not_block_next_boot(self):
        original = self.script.read_text()
        # Stop after writing authoritative readback, before invoking fallback.
        self.assertIn('regular "$readback" &&', original)
        self.script.write_text(original.replace('regular "$readback" &&', 'exit 1 # injected readback cut\nregular "$readback" &&'))
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.script.write_text(original)
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.lock.exists())

    def assert_publication_cut_resumes(self, command):
        original = self.script.read_text()
        self.assertIn(command, original)
        self.script.write_text(original.replace(command, 'exit 1 # injected temp cut\n        ' + command))
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        temporary = (self.root / ('.operator-recovery-check-status.boot-current' if 'check_tmp' in command else '.operator-recovery-state.boot-current'))
        self.assertTrue(temporary.is_file(), first.stderr)
        self.script.write_text(original)
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(temporary.exists())
        self.assertFalse(self.lock.exists())

    def test_crash_after_check_status_temp_resumes_next_boot(self):
        self.assert_publication_cut_resumes('$BB mv "$check_tmp" "$ROOT/check-status"')

    def test_crash_after_state_temp_resumes_next_boot(self):
        self.assert_publication_cut_resumes('$BB mv "$state_tmp" "$ROOT/state"')

    def test_post_publication_pre_unlock_cut_manual_resume_next_boot(self):
        original = self.script.read_text()
        self.script.write_text(original.replace('$BB rm "$OWNER" || refuse owner_release', 'exit 1 # injected cut after publication\n$BB rm "$OWNER" || refuse owner_release'))
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        self.assertIn('state=rolled-back', (self.root / 'state').read_text())
        self.assertTrue((self.lock / 'owner').exists())
        self.script.write_text(original)
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.lock.exists())

    def test_foreign_state_after_helper_never_changes_check_status(self):
        self.helper.write_text(self.helper.read_text().replace('printf "fallback-cleaned', 'printf "state=installing\\n" > "' + str(self.root / 'state') + '"; printf "fallback-cleaned'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        result = self.run_recovery(args)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'check-status').read_text(), 'status=reboot-pending\nlatest_version=v014\n')

    def test_non_worker_resumable_restart_state_refuses_before_helper(self):
        # The next-boot worker only handles restarting with health-confirm-failed.
        # An unqualified restart must not permit an irreversible partial cleanup.
        (self.root / 'state').write_text('state=restarting\n')
        result = self.run_recovery()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.assertFalse((self.root / 'rolled-back').exists())

    def run_candidate_worker(self, evidence=True):
        # Execute the candidate image's actual rollback/resume shell functions,
        # relocating only target paths. The helper is a strict fixture, not a
        # signature verifier; the real packaged helper is reviewed separately.
        source = CANDIDATE_INIT.read_text()
        start = source.index('    ota_rollback_publish_terminal()\n')
        end = source.index('    running_slot=$($BB sed -n', start)
        fragment = source[start:end]
        self.assertIn('ota_rollback_resume_install_lock()', fragment)
        self.assertIn('ota_rollback_resume_rollback_evidence()', fragment)
        fragment = fragment.replace('/data/libreecho/update', str(self.root))
        fragment = fragment.replace('/run/libreecho', str(self.runroot))
        fragment = fragment.replace('/proc/sys/kernel/random/boot_id', str(self.proc / 'boot_id'))
        fragment = fragment.replace('/usr/local/sbin/libreecho-feature-transaction', str(self.helper))
        worker = self.base / 'worker.sh'
        worker.write_text('BB=' + BB + '\nlog() { :; }\nselected_slot=b\n' + fragment + '\nota_rollback_resume_terminal\n')
        self.helper.write_text('#!/bin/sh\n[ "$1" = rollback-evidence ] || exit 1\n'
                               + ('[ -f "' + str(self.root / 'staging/manifest') + '" ] && [ -f "' + str(self.root / 'feature-commit') + '" ] || exit 1\n' if evidence else 'exit 1\n')
                               + 'printf "rollback-evidence transaction_id=incident-014 slot=a\\n"\n')
        self.helper.chmod(0o700)
        return subprocess.run([BB, 'sh', str(worker)], capture_output=True, text=True, timeout=15)

    def test_candidate_worker_reclaims_operator_tag_and_authenticated_one_sided_journal(self):
        # Simulate the helper's real order: history, pending unlink, journal unlink.
        original = self.helper.read_text()
        self.helper.write_text(original.replace('rm -f "' + str(self.root / 'pending') + '" "' + str(self.root / 'feature-commit') + '"',
            'rm -f "' + str(self.root / 'pending') + '"; exit 1; rm -f "' + str(self.root / 'feature-commit') + '"'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        first = self.run_recovery(args)
        self.assertNotEqual(first.returncode, 0)
        self.assertFalse((self.root / 'pending').exists())
        self.assertTrue((self.root / 'feature-commit').exists())
        self.assertEqual((self.lock / 'owner').read_text().splitlines()[0], 'owner=rollback-resume')
        # The fixture starts with this exact worker-compatible restart record.
        (self.proc / 'boot_id').write_text('boot-next\n')
        worker = self.run_candidate_worker()
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.root / 'feature-commit').exists())
        self.assertFalse((self.root / 'staging').exists())
        self.assertIn('state=rolled-back', (self.root / 'state').read_text())
        self.assertIn('status=update-held-after-rollback', (self.root / 'check-status').read_text())

    def test_candidate_worker_refuses_unproven_one_sided_journal(self):
        self.helper.write_text(self.helper.read_text().replace('rm -f "' + str(self.root / 'pending') + '" "' + str(self.root / 'feature-commit') + '"',
            'rm -f "' + str(self.root / 'pending') + '"; exit 1; rm -f "' + str(self.root / 'feature-commit') + '"'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        self.assertNotEqual(self.run_recovery(args).returncode, 0)
        # The fixture starts with this exact worker-compatible restart record.
        (self.proc / 'boot_id').write_text('boot-next\n')
        worker = self.run_candidate_worker(evidence=False)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertTrue((self.root / 'feature-commit').exists())
        self.assertTrue((self.root / 'staging').exists())
        self.assertIn('state=restarting', (self.root / 'state').read_text())

    def test_status_publication_failure_preserves_owner_and_history(self):
        (self.root / 'check-status.tmp').mkdir()
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.lock / 'owner').exists())
        self.assertTrue((self.root / 'rolled-back').exists())
        self.assertIn('reboot-pending', (self.root / 'check-status').read_text())

    def test_wrong_helper_hash_refused(self):
        args = self.args.copy()
        args[3] = '0' * 64
        self.assertNotEqual(self.run_recovery(args).returncode, 0)
        self.assertTrue((self.root / 'pending').exists())

    def test_helper_refusal_does_not_publish_or_remove_transaction(self):
        self.helper.write_text('#!/bin/sh\nprintf "ERROR:signature\\n" >&2\nexit 1\n')
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        r = self.run_recovery(args)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.assertTrue((self.lock / 'owner').exists())
        self.assertEqual((self.root / 'state').read_text(), 'schema=1\nstate=restarting\ndetail=health-confirm-failed:test\n')
        self.assertNotIn('signature', r.stderr)

    def test_prior_boot_tag_bound_to_transaction(self):
        (self.lock / 'owner').write_text('owner=operator-recovery\nboot_id=boot-prior\ntransaction_id=other\n')
        self.assertNotEqual(self.run_recovery().returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        self.assertTrue((self.lock / 'owner').exists())

    def test_resume_rejects_changed_history(self):
        (self.root / 'check-status.tmp').mkdir()
        self.assertNotEqual(self.run_recovery().returncode, 0)
        (self.root / 'check-status.tmp').rmdir()
        (self.root / 'rolled-back').write_text('transaction_id=foreign\n')
        (self.proc / 'boot_id').write_text('boot-next\n')
        self.assertNotEqual(self.run_recovery().returncode, 0)
        self.assertTrue((self.lock / 'owner').exists())
        self.assertIn('reboot-pending', (self.root / 'check-status').read_text())

    def test_interrupted_before_helper_resumes_next_boot(self):
        sentinel = self.base / 'stop-helper'
        sentinel.touch()
        self.helper.write_text(self.helper.read_text().replace('fallback) cp', 'fallback) [ ! -e "' + str(sentinel) + '" ] || exit 1; cp'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        self.assertNotEqual(self.run_recovery(args).returncode, 0)
        self.assertTrue((self.root / 'pending').exists())
        sentinel.unlink()
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery(args)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.lock.exists())

    def test_stale_readback_replaced_before_helper(self):
        # The stand-in helper checks the readback it received, not a source grep.
        self.helper.write_text(self.helper.read_text().replace('fallback) cp', 'fallback) [ "$(sed -n "s/^selected_slot=//p" "' + str(self.root / 'staging/bootctl.readback') + '")" = b ] || exit 1; cp'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        r = self.run_recovery(args)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bootctl_failure_refuses_before_helper(self):
        self.bootctl.write_text('#!/bin/sh\nexit 1\n')
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.root / 'pending').exists())

    def assert_resumed_bad_bootctl_releases_fetch_lock(self, bootctl_body):
        (self.root / 'check-status.tmp').mkdir()
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        self.assertFalse((self.root / 'staging').exists())
        (self.root / 'check-status.tmp').rmdir()
        (self.proc / 'boot_id').write_text('boot-next\n')
        original = self.bootctl.read_text()
        self.bootctl.write_text(bootctl_body)
        refused = self.run_recovery()
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.runroot / 'fetch.lock').exists(), refused.stderr)
        self.bootctl.write_text(original)
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.lock.exists())

    def test_resumed_bootctl_exit_failure_releases_fetch_lock(self):
        self.assert_resumed_bad_bootctl_releases_fetch_lock('#!/bin/sh\nexit 1\n')

    def test_resumed_bootctl_invalid_readback_releases_fetch_lock(self):
        self.assert_resumed_bad_bootctl_releases_fetch_lock('#!/bin/sh\nprintf "selected_slot=a\\nslot_a_success=1\\nslot_b_success=0\\n"\n')

    def test_sigterm_during_bootctl_releases_fetch_lock(self):
        self.assert_sigterm_during_bootctl_releases_fetch_lock(group=True)

    def test_parent_only_sigterm_during_bootctl_releases_fetch_lock(self):
        self.assert_sigterm_during_bootctl_releases_fetch_lock(group=False)

    def test_late_sigterm_after_unlock_does_not_hide_completion(self):
        original = self.script.read_text()
        line = '$BB rmdir "$RUN/fetch.lock" || refuse fetch_release'
        self.assertIn(line, original)
        self.script.write_text(original.replace(line, line + '\n$BB kill -TERM $$ # injected late signal'))
        result = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RECOVERY_COMPLETE transaction_id=incident-014', result.stdout)
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.runroot / 'fetch.lock').exists())

    def assert_sigterm_during_bootctl_releases_fetch_lock(self, group):
        marker = self.base / 'bootctl-started'
        original = self.bootctl.read_text()
        self.bootctl.write_text(f'#!/bin/sh\n: > "{marker}"\nexec sleep 10\n')
        p = subprocess.Popen([BB, 'sh', str(self.script), *self.args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), 'bootctl status did not start')
            if group:
                os.killpg(p.pid, signal.SIGTERM)
            else:
                os.kill(p.pid, signal.SIGTERM)
            _, stderr = p.communicate(timeout=1)
            self.assertNotEqual(p.returncode, 0, stderr)
            self.assertFalse((self.runroot / 'fetch.lock').exists(), stderr)
            self.assertTrue((self.lock / 'owner').exists())
            self.assertTrue((self.root / 'pending').exists())
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if p.poll() is None:
                p.communicate(timeout=5)
        self.bootctl.write_text(original)
        (self.proc / 'boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_running_selected_confirmed_and_failed_candidate_unconditional(self):
        for cmdline, status in [
            ('androidboot.slot_suffix=_a', 'selected_slot=b\nslot_a_success=0\nslot_b_success=1\n'),
            ('androidboot.slot_suffix=_b', 'selected_slot=a\nslot_a_success=0\nslot_b_success=1\n'),
            ('androidboot.slot_suffix=_b', 'selected_slot=b\nslot_a_success=1\nslot_b_success=1\n'),
            ('androidboot.slot_suffix=_b', 'selected_slot=b\nslot_a_success=0\nslot_b_success=0\n'),
        ]:
            with self.subTest(cmdline=cmdline, status=status):
                self.running.write_text(cmdline + '\n')
                self.bootctl.write_text('#!/bin/sh\nprintf ' + repr(status) + '\n')
                r = self.run_recovery()
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertTrue((self.root / 'pending').exists())

    def test_partial_owner_write_crash_does_not_poison_lock(self):
        original = self.script.read_text()
        self.script.write_text(original.replace('(umask 077; set -C; printf ', '(umask 077; set -C; printf partial > "$owner_tmp"; exit 1; printf '))
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        self.assertFalse((self.lock / 'owner').exists())
        self.assertEqual((self.root / '.operator-recovery-owner.boot-current').read_text(), 'partial')
        self.assertTrue((self.root / 'pending').exists())
        self.script.write_text(original)
        self.proc.joinpath('boot_id').write_text('boot-next\n')
        second = self.run_recovery()
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_crash_after_owner_link_resumes_only_matching_prior_boot_inode(self):
        original = self.script.read_text()
        self.script.write_text(original.replace('$BB rm "$owner_tmp" || refuse owner_link_cleanup', 'exit 1 # injected death after atomic link'))
        first = self.run_recovery()
        self.assertNotEqual(first.returncode, 0)
        owner = self.lock / 'owner'
        temporary = self.root / '.operator-recovery-owner.boot-current'
        self.assertEqual(owner.stat().st_ino, temporary.stat().st_ino)
        self.assertEqual(owner.stat().st_nlink, 2)
        self.script.write_text(original)
        self.assertNotEqual(self.run_recovery().returncode, 0)  # same boot is busy
        self.proc.joinpath('boot_id').write_text('boot-next\n')
        resumed = self.run_recovery()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(temporary.exists())
        self.assertFalse(self.lock.exists())

    def test_foreign_double_link_owner_is_preserved(self):
        foreign = self.base / 'foreign-owner'
        foreign.write_text('owner=foreign\nboot_id=boot-prior\n')
        os.link(foreign, self.lock / 'owner')
        self.assertNotEqual(self.run_recovery().returncode, 0)
        self.assertEqual(foreign.stat().st_nlink, 2)
        self.assertTrue((self.root / 'pending').exists())

    def test_foreign_state_after_helper_is_not_overwritten(self):
        foreign = 'schema=1\nstate=installing\nprogress=4\n'
        self.helper.write_text(self.helper.read_text().replace('printf "fallback-cleaned', 'printf "schema=1\\nstate=installing\\nprogress=4\\n" > "' + str(self.root / 'state') + '"; printf "fallback-cleaned'))
        args = self.args.copy()
        args[3] = digest(self.helper.read_bytes())
        self.script.write_text(self.script.read_text().replace(self.args[3], args[3]))
        r = self.run_recovery(args)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.root / 'state').read_text(), foreign)
        self.assertTrue((self.lock / 'owner').exists())

    def test_foreign_state_before_helper_refuses_without_removing_pending(self):
        foreign = 'schema=1\nstate=installing\nprogress=4\n'
        (self.root / 'state').write_text(foreign)
        r = self.run_recovery()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.root / 'state').read_text(), foreign)
        self.assertTrue((self.root / 'pending').exists())


if __name__ == '__main__':
    unittest.main()
