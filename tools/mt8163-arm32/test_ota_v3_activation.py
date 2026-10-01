"""Generation switching and actual BusyBox entry-point activation policy."""
import hashlib
from pathlib import Path
import subprocess

from test_ota_v3_convergence import ConvergenceTests, GENERATION
from test_ota_v3_manifest import FEATURES, TOOLS

TRANSACTION = TOOLS / 'initramfs/libreecho-feature-transaction'


class ActivationTests(ConvergenceTests):
    # Convergence tests inherited intentionally also exercise the common builder.
    def setUp(self):
        super().setUp()
        self.boot = self.root / 'boot_b'
        self.boot.write_bytes(b'boot-target')
        self.boot_a = self.root / 'boot_a'
        self.boot_a.write_bytes(b'boot-prior')
        self.text = self.text.replace('boot_sha256=' + 'a' * 64,
                                     'boot_sha256=' + hashlib.sha256(self.boot.read_bytes()).hexdigest())
        self.text = self.text.replace('daemon_sha256=' + 'b' * 64,
                                     'daemon_sha256=' + hashlib.sha256(b'daemon').hexdigest())
        self.p, self.s = self.signed(self.text)
        self.cmdline = self.root / 'cmdline'
        self.cmdline.write_text('androidboot.slot_suffix=_b\n')
        self.bcb = self.root / 'bcb'
        self.set_bcb('b', '0')
        self.mountinfo = self.root / 'mountinfo'
        self.mountinfo.touch()
        self.mount_log = self.root / 'mounts'
        self.runroot = self.root / 'run'
        self.loops = self.root / 'loops'
        bootctl = self.root / 'bootctl'
        bootctl.write_text('#!/bin/sh\ncat "$BCB_SOURCE"\n')
        bootctl.chmod(0o755)
        mount = self.root / 'mount'
        mount.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
source, target=sys.argv[-2:]
with Path(os.environ['MOUNT_LOG']).open('a') as file: file.write(' '.join(sys.argv[1:])+'\\n')
info=Path(os.environ['MOUNTINFO_FILE'])
n=len(info.read_text().splitlines())
loop=Path(os.environ['LOOP_SYS_ROOT']) / ('7:'+str(n)) / 'loop'
loop.mkdir(parents=True, exist_ok=True)
(loop/'backing_file').write_text(source+'\\n')
(loop/'offset').write_text('0\\n')
(loop/'sizelimit').write_text('0\\n')
with info.open('a') as file: file.write(f'{n+1} 0 7:{n} / {target} ro,nosuid,nodev - squashfs /dev/loop{n} ro\\n')
for daemon in ('libreecho-audio-engine','libreecho-ttsd','libreecho-waked','libreecho-sttd','libreecho-agentd'):
    p=Path(target)/'usr/local/sbin'/daemon
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_bytes(b'daemon')
''')
        mount.chmod(0o755)
        self.env.update(GENERATION_TRANSACTION=str(TOOLS / 'initramfs/libreecho-generation-transaction'), BOOTCTL=str(bootctl), BCB_SOURCE=str(self.bcb), CMDLINE_FILE=str(self.cmdline),
                        BOOT_A=str(self.boot_a), BOOT_B=str(self.boot), MOUNT=str(mount),
                        MOUNTINFO_FILE=str(self.mountinfo), LOOP_SYS_ROOT=str(self.loops),
                        RUN_ROOT=str(self.runroot), MOUNT_LOG=str(self.mount_log))

    def set_bcb(self, slot, success):
        self.bcb.write_text(f'selected_slot={slot}\nslot_a_success=1\nslot_b_success={success}\n')

    def prepared(self):
        result = self.run_assembly()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.control / 'current').write_text('prior\n')
        (self.control / 'pending').write_text('schema=3\nslot=b\ntransaction_id=test-target\nmanifest_sha256=' + hashlib.sha256(self.p.read_bytes()).hexdigest() + '\n')

    def verb(self, verb):
        return subprocess.run(['/bin/busybox', 'sh', str(TRANSACTION), verb],
                              env=self.env, capture_output=True, text=True)

    def test_boot_daemon_and_mount_options_mismatch_fail_closed(self):
        self.prepared()
        original = self.boot.read_bytes()
        self.boot.write_bytes(b'wrong boot')
        self.assertNotEqual(self.verb('activate').returncode, 0)
        self.assertFalse(self.mount_log.exists())
        self.boot.write_bytes(original)
        self.assertEqual(self.verb('activate').returncode, 0)
        daemon = self.runroot / 'libreecho/features/assistant/root/usr/local/sbin/libreecho-agentd'
        daemon.write_bytes(b'stale daemon')
        self.assertNotEqual(self.verb('activate').returncode, 0)
        daemon.write_bytes(b'daemon')
        self.mountinfo.write_text(self.mountinfo.read_text().replace('ro,nosuid,nodev', 'rw,nosuid,nodev'))
        self.assertNotEqual(self.verb('activate').returncode, 0)

    def test_ambiguous_bcb_and_selected_running_mismatch_fail_closed(self):
        self.prepared()
        for bcb in ('selected_slot=a\nslot_a_success=0\n',
                    'selected_slot=b\nselected_slot=a\nslot_b_success=0\n',
                    'selected_slot=b\nslot_b_success=2\n'):
            self.bcb.write_text(bcb)
            result = self.verb('activate')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.mount_log.exists())

    def test_activate_exact_pending_readonly_loop_mounts(self):
        self.prepared()
        result = self.verb('activate')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.mount_log.read_text().splitlines()
        self.assertEqual(len(calls), 5)
        for f, call in zip(FEATURES, calls):
            self.assertIn('ro,loop,nosuid,nodev', call)
            self.assertIn(str(self.generations / f'test-target/features/{f}/payload.squashfs'), call)
        self.assertEqual(self.verb('activate').returncode, 0)
        self.assertEqual(len(self.mount_log.read_text().splitlines()), 5)

    def test_empty_or_missing_slot_suffix_uses_validated_bcb(self):
        self.prepared()
        for cmdline in ('androidboot.slot_suffix=_\n', 'console=ttyS0\n'):
            self.cmdline.write_text(cmdline)
            result = self.verb('activate')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_activate_refuses_wrong_slot(self):
        self.prepared()
        self.cmdline.write_text('androidboot.slot_suffix=_a\n')
        self.assertNotEqual(self.verb('activate').returncode, 0)
        self.assertFalse(self.mount_log.exists())

    def test_commit_refuses_unconfirmed(self):
        self.prepared()
        self.assertNotEqual(self.verb('commit').returncode, 0)
        self.assertEqual((self.control / 'current').read_text(), 'prior\n')

    def test_commit_then_resume_keeps_previous(self):
        self.prepared()
        self.set_bcb('b', '1')
        result = self.verb('commit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.control / 'previous').read_text(), 'prior\n')
        self.assertEqual((self.control / 'current').read_text(), 'test-target\n')
        self.assertFalse((self.control / 'pending').exists())
        self.assertEqual(self.verb('commit').returncode, 0)
        self.assertEqual((self.control / 'previous').read_text(), 'prior\n')
        self.assertIn('schema=3', (self.control / 'installed').read_text())
        self.assertIn('transaction_id=test-target', (self.control / 'installed').read_text())

    def test_commit_resume_after_current_rename_keeps_previous(self):
        self.prepared()
        (self.control / 'previous').write_text('prior\n')
        (self.control / 'current').write_text('test-target\n')
        self.set_bcb('b', '1')
        self.assertEqual(self.verb('commit').returncode, 0)
        self.assertEqual((self.control / 'previous').read_text(), 'prior\n')
        self.assertFalse((self.control / 'pending').exists())

    def test_rollback_keeps_current_and_candidate_evidence(self):
        self.prepared()
        self.cmdline.write_text('androidboot.slot_suffix=_a\n')
        self.set_bcb('a', '0')
        result = self.verb('rollback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.control / 'current').read_text(), 'prior\n')
        self.assertFalse((self.control / 'pending').exists())
        self.assertFalse((self.generations / 'test-target').exists())

    def test_committed_activation_refuses_running_boot_mismatch(self):
        self.prepared()
        (self.control / 'current').write_text('test-target\n')
        (self.control / 'pending').unlink()
        self.boot.write_bytes(b'other release boot')
        result = self.verb('activate-committed')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('committed-boot-hash', result.stderr)
        self.assertFalse(self.mount_log.exists())

    def test_activate_committed_rehashes_payload(self):
        self.prepared()
        (self.control / 'current').write_text('test-target\n')
        (self.control / 'pending').unlink()
        result = self.verb('activate-committed')
        self.assertEqual(result.returncode, 0, result.stderr)
        p = self.generations / 'test-target/features/stt/payload.squashfs'
        p.chmod(0o600)
        p.write_bytes(b'corrupted')
        self.assertNotEqual(self.verb('activate-committed').returncode, 0)

    def test_https_client_ignores_corrupt_unrelated_feature(self):
        self.prepared()
        (self.control / 'current').write_text('test-target\n')
        (self.control / 'pending').unlink()
        payload = self.generations / 'test-target/features/stt/payload.squashfs'
        payload.chmod(0o600); payload.write_bytes(b'corrupted')
        result = self.verb('https-client')
        self.assertEqual(result.returncode, 0, result.stderr)
        assistant = self.generations / 'test-target/features/assistant/payload.squashfs'
        assistant.chmod(0o600); assistant.write_bytes(b'corrupted')
        result = self.verb('https-client')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('https-transport-corrupt', result.stderr)

    def test_https_client_mount_is_bound_to_signed_current_generation(self):
        self.prepared()
        (self.control / 'current').write_text('test-target\n')
        (self.control / 'pending').unlink()
        result = self.verb('https-client')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.mount_log.read_text().splitlines()), 1)
        self.assertIn(str(self.generations / 'test-target/features/assistant/payload.squashfs'), self.mount_log.read_text())
        (self.loops / '7:0/loop/backing_file').write_text('/wrong.squashfs\n')
        self.assertNotEqual(self.verb('https-client').returncode, 0)

    def test_generation_lock_blocks_gc_and_commit(self):
        self.prepared()
        import os
        lock = self.control / 'generation.lock'
        lock.mkdir()
        (lock / 'owner').write_text(f'{os.getpid()} ' + Path('/proc/sys/kernel/random/boot_id').read_text())
        self.set_bcb('b', '1')
        for verb in ('gc', 'commit'):
            self.assertNotEqual(self.verb(verb).returncode, 0)
        self.assertTrue((self.control / 'pending').exists())
        self.assertEqual((self.control / 'current').read_text(), 'prior\n')

    def test_ownerless_lock_publication_crash_recovers(self):
        self.prepared()
        # Legacy publication window plus abandoned prepublication temp directory.
        (self.control / 'generation.lock').mkdir()
        (self.control / 'generation.lock.tmp.999999').mkdir()
        self.set_bcb('b', '1')
        result = self.verb('commit')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dead_owner_lock_is_recovered(self):
        self.prepared()
        lock = self.control / 'generation.lock'
        lock.mkdir()
        (lock / 'owner').write_text('99999999 ' + Path('/proc/sys/kernel/random/boot_id').read_text().strip() + '\n')
        self.set_bcb('b', '1')
        result = self.verb('commit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_prior_boot_lock_is_recovered_but_live_owner_is_not(self):
        import os
        self.prepared()
        lock = self.control / 'generation.lock'
        lock.mkdir()
        (lock / 'owner').write_text(str(os.getpid()) + ' previous-boot\n')
        self.assertEqual(self.verb('activate').returncode, 0)
        lock.mkdir()
        (lock / 'owner').write_text(str(os.getpid()) + ' ' + Path('/proc/sys/kernel/random/boot_id').read_text().strip() + '\n')
        result = self.verb('gc')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('generation-busy', result.stderr)

    def test_commit_sigkill_at_each_durable_boundary_resumes(self):
        engine = TOOLS / 'initramfs/libreecho-generation-transaction'
        for boundary in (1, 2, 3, 4):
            with self.subTest(boundary=boundary):
                self.prepared()
                self.set_bcb('b', '1')
                count = self.root / 'sync-count'
                count.write_text('0')
                script = self.root / 'crash-engine'
                source = engine.read_text().replace(
                    'sync_file() { $BB sync 2>/dev/null || fail sync-failed; }',
                    'sync_file() { $BB sync; n=$(( $(cat "$SYNC_COUNT") + 1 )); echo "$n" > "$SYNC_COUNT"; [ "$n" != "$CRASH_BOUNDARY" ] || kill -KILL $$; }')
                script.write_text(source)
                result = subprocess.run(['/bin/busybox', 'sh', str(script), 'commit'],
                    env=dict(self.env, SYNC_COUNT=str(count), CRASH_BOUNDARY=str(boundary)), capture_output=True)
                self.assertEqual(result.returncode, -9)
                result = self.verb('commit')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.control / 'current').read_text(), 'test-target\n')
                self.assertEqual((self.control / 'previous').read_text(), 'prior\n')
                self.assertFalse((self.control / 'pending').exists())

    def test_loop_backing_mismatch_is_rejected(self):
        self.prepared()
        self.assertEqual(self.verb('activate').returncode, 0)
        (self.loops / '7:0/loop/backing_file').write_text('/wrong/payload.squashfs\n')
        self.assertNotEqual(self.verb('activate').returncode, 0)

    def test_gc_retains_assembled_pin_until_pending_publication(self):
        self.assertEqual(self.run_assembly().returncode, 0)
        result = self.verb('gc')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.generations / 'test-target/COMPLETE').exists())
        self.assertTrue((self.generations / 'test-target.pin').is_file())

    def test_commit_collects_obsolete_generations(self):
        self.prepared()
        obsolete = self.generations / 'obsolete'
        obsolete.mkdir(); (obsolete / 'bytes').write_bytes(b'obsolete')
        self.set_bcb('b', '1')
        result = self.verb('commit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(obsolete.exists())
        self.assertFalse((self.generations / 'test-target.pin').exists())

    def test_gc_keeps_control_targets_never_config(self):
        self.prepared()
        for tx in ('prior', 'previous', 'obsolete'):
            d = self.generations / tx
            d.mkdir()
            (d / 'evidence').write_text(tx)
        (self.control / 'previous').write_text('previous\n')
        config = self.data / 'config/web-config.json'
        config.parent.mkdir()
        config.write_text('{"schema":1}')
        result = self.verb('gc')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.generations.iterdir()), ['previous', 'prior', 'test-target', 'test-target.pin'])
        self.assertEqual(config.read_text(), '{"schema":1}')

    def test_v3_engine_has_no_overlay_or_history_actions(self):
        source = (TOOLS / 'initramfs/libreecho-generation-transaction').read_text() if (TOOLS / 'initramfs/libreecho-generation-transaction').exists() else TRANSACTION.read_text()
        for forbidden in ('preserve', 'runtime', 'base_payload', 'old_payload', 'mount --bind'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    import unittest
    unittest.main()
