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
        (lock / 'reclaim').mkdir()
        self.set_bcb('b', '1')
        for verb in ('gc', 'commit'):
            self.assertNotEqual(self.verb(verb).returncode, 0)
        self.assertTrue((lock / 'owner').is_file())
        self.assertTrue((lock / 'reclaim').is_dir())
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

    def test_sigkill_before_owner_publication_leaves_no_visible_lock(self):
        for name, args in (('libreecho-generation', ['assemble', str(self.p), str(self.s)]),
                           ('libreecho-generation-transaction', ['gc'])):
            with self.subTest(helper=name):
                wrapper = self.root / 'crash-busybox'
                wrapper.write_text('''#!/bin/sh
last=
for argument do last=$argument; done
if [ "$1" = mkdir ]; then
    case "$last" in *.lock.tmp.*)
        /bin/busybox "$@" || exit 1
        kill -KILL "$PPID"
        exit 1;;
    esac
fi
exec /bin/busybox "$@"
''')
                wrapper.chmod(0o755)
                source = (TOOLS / 'initramfs' / name).read_text().replace('BB=/bin/busybox', 'BB=' + str(wrapper))
                script = self.root / 'crash-lock'; script.write_text(source)
                result = subprocess.run(['/bin/busybox', 'sh', str(script), *args], env=self.env, capture_output=True)
                self.assertEqual(result.returncode, -9)
                self.assertFalse((self.control / 'generation.lock').exists())
                result = self.verb('gc')
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_reclamation_crash_boundaries_recover(self):
        import shutil
        for name in ('libreecho-generation', 'libreecho-generation-transaction'):
            for operation in ('mkdir-reclaim', 'rm-owner', 'rmdir-reclaim', 'rmdir-lock'):
                for phase in ('before', 'after'):
                    with self.subTest(helper=name, operation=operation, phase=phase):
                        lock = self.control / 'generation.lock'
                        if lock.exists(): shutil.rmtree(lock)
                        lock.mkdir()
                        (lock / 'owner').write_text('99999999 ' + Path('/proc/sys/kernel/random/boot_id').read_text())
                        if operation != 'mkdir-reclaim': (lock / 'reclaim').mkdir()
                        wrapper = self.root / 'reclaim-busybox'
                        wrapper.write_text('''#!/bin/sh
last=
for argument do last=$argument; done
match=0
case "$FAULT:$1:$last" in
 mkdir-reclaim:mkdir:*/generation.lock/reclaim|rm-owner:rm:owner|rm-owner:rm:*/generation.lock/owner|rmdir-reclaim:rmdir:reclaim|rmdir-reclaim:rmdir:*/generation.lock/reclaim|rmdir-lock:rmdir:*/generation.lock) match=1;;
esac
if [ "$match" = 1 ] && [ "$PHASE" = before ]; then kill -KILL "$LOCK_PID"; exit 1; fi
/bin/busybox "$@"
rc=$?
if [ "$match" = 1 ] && [ "$PHASE" = after ] && [ "$rc" = 0 ]; then kill -KILL "$LOCK_PID"; exit 1; fi
exit "$rc"
''')
                        wrapper.chmod(0o755)
                        source = (TOOLS / 'initramfs' / name).read_text()
                        # Legacy marker publication is a deployed crash state; recreate
                        # that boundary even once new helpers no longer publish markers.
                        if operation == 'mkdir-reclaim':
                            source = source[:source.index('generation_lock()')] + '\n$BB mkdir "$ROOT/generation.lock/reclaim"\n'
                        else:
                            source = source[:source.rfind('case "${1:-}" in')] if name == 'libreecho-generation' else source.split('# Observation commands')[0]
                            source += '\ngeneration_lock\n'
                        script = self.root / 'reclaim-helper'
                        source = source.replace('BB=/bin/busybox', 'BB=' + str(wrapper))
                        source = source.replace('BB=' + str(wrapper), 'BB=' + str(wrapper) + '\nexport LOCK_PID=$$')
                        script.write_text(source)
                        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=dict(self.env, FAULT=operation, PHASE=phase), capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, -9)
                        recovered = self.verb('gc')
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        self.assertFalse(lock.exists())

    def test_simultaneous_atomic_lock_publication_has_one_winner(self):
        import time
        wrapper = self.root / 'publish-busybox'
        wrapper.write_text('''#!/bin/sh
if [ "$1" = mv ] && [ "$2" = -T ]; then
 /bin/busybox touch "$BARRIER/$LABEL.ready"
 while [ ! -f "$BARRIER/go" ]; do /bin/busybox sleep .01; done
fi
exec /bin/busybox "$@"
''')
        wrapper.chmod(0o755)
        processes = []
        try:
            for label, name in (('first', 'libreecho-generation'), ('second', 'libreecho-generation-transaction')):
                source = (TOOLS / 'initramfs' / name).read_text()
                source = source[:source.rfind('case "${1:-}" in')] if label == 'first' else source.split('# Observation commands')[0]
                source = source.replace('BB=/bin/busybox', 'BB=' + str(wrapper))
                source += '\ngeneration_lock\n/bin/busybox touch "$BARRIER/$LABEL.entered"\nwhile [ ! -f "$BARRIER/finish" ]; do /bin/busybox sleep .01; done\n'
                script = self.root / (label + '-publish'); script.write_text(source)
                processes.append(subprocess.Popen(['/bin/busybox', 'sh', str(script)], env=dict(self.env, BARRIER=str(self.root), LABEL=label), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 5
            while len(list(self.root.glob('*.ready'))) != 2 and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual(len(list(self.root.glob('*.ready'))), 2)
            (self.root / 'go').touch()
            deadline = time.monotonic() + 5
            while not any(p.poll() is not None for p in processes) and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual(sum(p.poll() is not None for p in processes), 1)
            self.assertEqual(len(list(self.root.glob('*.entered'))), 1)
            (self.root / 'finish').touch()
            results = []
            for process in processes:
                process.communicate(timeout=5); results.append(process.returncode)
            self.assertEqual(sorted(results), [0, 1])
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)

    def test_concurrent_reclaimer_cannot_remove_new_live_owner(self):
        import time
        lock = self.control / 'generation.lock'
        lock.mkdir()
        (lock / 'owner').write_text('99999999 ' + Path('/proc/sys/kernel/random/boot_id').read_text())
        wrapper = self.root / 'race-busybox'
        wrapper.write_text('''#!/bin/sh
if [ "$1" = rm ] && [ "$3" = owner ]; then
 /bin/busybox touch "$BARRIER/$LABEL.ready"
 while [ ! -f "$BARRIER/$LABEL.go" ]; do /bin/busybox sleep .01; done
fi
exec /bin/busybox "$@"
''')
        wrapper.chmod(0o755)
        processes = []
        try:
            for label, name in (('first', 'libreecho-generation'), ('second', 'libreecho-generation-transaction')):
                source = (TOOLS / 'initramfs' / name).read_text()
                source = source[:source.rfind('case "${1:-}" in')] if label == 'first' else source.split('# Observation commands')[0]
                source = source.replace('BB=/bin/busybox', 'BB=' + str(wrapper))
                source += '\ngeneration_lock\n/bin/busybox touch "$BARRIER/$LABEL.entered"\nwhile [ ! -f "$BARRIER/finish" ]; do /bin/busybox sleep .01; done\n'
                script = self.root / (label + '-lock'); script.write_text(source)
                processes.append(subprocess.Popen(['/bin/busybox', 'sh', str(script)], env=dict(self.env, BARRIER=str(self.root), LABEL=label), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            def wait_for(path):
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline: time.sleep(.01)
                self.assertTrue(path.exists(), str(path))
            for label in ('first', 'second'): wait_for(self.root / (label + '.ready'))
            (self.root / 'first.go').touch()
            wait_for(self.root / 'first.entered')
            live_owner = (lock / 'owner').read_text()
            (self.root / 'second.go').touch()
            _, stderr = processes[1].communicate(timeout=5)
            self.assertNotEqual(processes[1].returncode, 0, stderr)
            self.assertEqual((lock / 'owner').read_text(), live_owner)
            self.assertFalse((self.root / 'second.entered').exists())
            self.assertNotEqual(self.verb('gc').returncode, 0)
            (self.root / 'finish').touch()
            _, stderr = processes[0].communicate(timeout=5)
            self.assertEqual(processes[0].returncode, 0, stderr)
            self.assertFalse(lock.exists())
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)

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
