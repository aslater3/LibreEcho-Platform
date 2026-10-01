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
        self.env.update(BOOTCTL=str(bootctl), BCB_SOURCE=str(self.bcb), CMDLINE_FILE=str(self.cmdline),
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
        self.assertTrue((self.generations / 'test-target/COMPLETE').exists())

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

    def test_loop_backing_mismatch_is_rejected(self):
        self.prepared()
        self.assertEqual(self.verb('activate').returncode, 0)
        (self.loops / '7:0/loop/backing_file').write_text('/wrong/payload.squashfs\n')
        self.assertNotEqual(self.verb('activate').returncode, 0)

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
        self.assertEqual(sorted(p.name for p in self.generations.iterdir()), ['previous', 'prior', 'test-target'])
        self.assertEqual(config.read_text(), '{"schema":1}')

    def test_v3_engine_has_no_overlay_or_history_actions(self):
        source = (TOOLS / 'initramfs/libreecho-generation-transaction').read_text() if (TOOLS / 'initramfs/libreecho-generation-transaction').exists() else TRANSACTION.read_text()
        for forbidden in ('preserve', 'runtime', 'base_payload', 'old_payload', 'mount --bind'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    import unittest
    unittest.main()
