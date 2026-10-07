"""Issue #231: the pinned Biscuit chain always boots the boot_a store.

Measured on hardware: with different images in boot_a/boot_b, LK loaded boot_a
whichever slot the BCB selected and only reported the selected suffix. An OTA
that wrote the inactive store (boot_b) therefore never changed the running
kernel, yet confirmed. These tests pin the fixed contract:

* bootctl reports ``boot_store_mode=fixed-a`` and maps both slots onto boot_a
  for the pinned layout, while the Amonet (Radar) layout is unchanged;
* the updater backs the committed running image up to boot_b and writes the
  candidate to boot_a on fixed-a, and still writes the inactive slot otherwise;
* the generation transaction verifies the store that actually boots and
  converges boot_a back onto the committed image after a rollback.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_ota_v3_activation as activation
from test_ota_v3_manifest import TOOLS

UPDATER = TOOLS / 'initramfs/libreecho-update'


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, **kwargs)


class BootctlStoreModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.work.name) / 'bootctl-partnames'
        result = run(['cc', '-Wall', '-Wextra', '-Werror', '-o', str(cls.binary),
                      str(TOOLS / 'ota/test_bootctl_partnames.c')])
        if result.returncode:
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def status(self, names, wrappers=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = {8: ('misc', 1025), 9: ('persist', 32768),
                       10: (names[0], 32768), 11: (names[1], 32768), 16: ('userdata', 2153472)}
            if wrappers:
                records.update({17: ('boot_a', 225280), 18: ('boot_b', 225280)})
            for index, (name, size) in records.items():
                directory = root / f'mmcblk0p{index}'
                directory.mkdir()
                (directory / 'size').write_text(str(size) + '\n')
                (directory / 'uevent').write_text('DEVTYPE=partition\nPARTNAME=' + name + '\n')
            result = run([str(self.binary), str(root), 'status'])
            self.assertEqual(result.returncode, 0, result.stderr)
            return dict(line.split('=', 1) for line in result.stdout.splitlines())

    def test_pinned_layout_maps_both_slots_onto_boot_a(self):
        status = self.status(('boot_a', 'boot_b'))
        self.assertEqual(status['boot_layout'], 'pinned')
        self.assertEqual(status['boot_store_mode'], 'fixed-a')
        self.assertEqual(status['slot_a_image'], '/dev/mmcblk0p10')
        self.assertEqual(status['slot_b_image'], '/dev/mmcblk0p10')
        self.assertEqual(status['backup_image'], '/dev/mmcblk0p11')

    def test_amonet_layout_keeps_slotted_stores(self):
        status = self.status(('boot_a_x', 'boot_b_x'), wrappers=True)
        self.assertEqual(status['boot_layout'], 'amonet')
        self.assertEqual(status['boot_store_mode'], 'slotted')
        self.assertEqual(status['slot_a_image'], '/dev/mmcblk0p10')
        self.assertEqual(status['slot_b_image'], '/dev/mmcblk0p11')
        self.assertNotIn('backup_image', status)


class UpdaterStoreSelectionTests(unittest.TestCase):
    """Run the updater's real slot/store functions against fake block stores."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dev = self.root / 'dev'
        self.sys = self.root / 'sys'
        self.dev.mkdir()
        for index, name in ((10, 'boot_a'), (11, 'boot_b')):
            (self.sys / f'mmcblk0p{index}').mkdir(parents=True)
            (self.sys / f'mmcblk0p{index}/size').write_text('32768\n')
            (self.sys / f'mmcblk0p{index}/uevent').write_text(f'PARTNAME={name}\n')
        self.boot_a = self.dev / 'mmcblk0p10'
        self.boot_b = self.dev / 'mmcblk0p11'
        self.update = self.root / 'update'
        self.staging = self.update / 'staging'
        self.staging.mkdir(parents=True)
        self.generations = self.root / 'generations'
        source = UPDATER.read_text()
        start = source.index('target_device_for_slot()')
        end = source.index('state_write()')
        functions = source[start:end]
        start = source.index('bootctl_status()')
        end = source.index('feature_file_sha256()')
        functions += source[start:end]
        # Regular files stand in for the block stores: substitute only the
        # block-device test, every PARTNAME and sector check stays real.
        self.assertEqual(functions.count('[ -b "$target_device" ]'), 1)
        functions = functions.replace('[ -b "$target_device" ]', '[ -f "$target_device" ]')
        self.script = self.root / 'harness.sh'
        self.script.write_text(
            'BB=/bin/busybox\n'
            f'BOOT_DEVICE_ROOT={self.dev}\nBOOT_SYS_ROOT={self.sys}\nBOOT_SECTORS=32768\n'
            f'STAGING={self.staging}\nUPDATE_ROOT={self.update}\nGENERATIONS={self.generations}\n'
            'BOOTCTL=$FAKE_BOOTCTL\n'
            'die() { echo "ERROR:$1" >&2; exit 1; }\n'
            + functions +
            'bootctl_status\n'
            'backup_running_boot\n'
            'echo "target=$target write=$write_device backup=$backup_device"\n'
            '$BB dd if="$NEW_IMAGE" of="$write_device" bs=4096 conv=fsync 2>/dev/null\n')
        self.bootctl = self.root / 'bootctl'
        self.new_image = self.root / 'new.img'
        self.new_image.write_bytes(b'candidate-image')

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, store_mode, selected='a'):
        inactive = 'b' if selected == 'a' else 'a'
        mode = f'boot_store_mode={store_mode}\n' if store_mode else ''
        self.bootctl.write_text('#!/bin/sh\nprintf "selected_slot=%s\\ninactive_slot=%s\\n'
                                f'slot_{selected}_success=1\\n{mode}"'
                                f' {selected} {inactive}\n')
        self.bootctl.chmod(0o755)
        env = os.environ | {'FAKE_BOOTCTL': str(self.bootctl), 'NEW_IMAGE': str(self.new_image)}
        return run(['/bin/busybox', 'sh', str(self.script)], env=env)

    def commit(self, image):
        (self.generations / 'prior').mkdir(parents=True)
        (self.generations / 'prior/target.manifest').write_text(
            'boot_sha256=' + hashlib.sha256(image).hexdigest() + '\n')
        (self.update / 'current').write_text('prior\n')

    def test_fixed_a_backs_up_committed_image_and_writes_boot_a(self):
        self.boot_a.write_bytes(b'running-image')
        self.boot_b.write_bytes(b'stale-image')
        self.commit(b'running-image')
        for selected, target in (('a', 'b'), ('b', 'a')):
            self.boot_a.write_bytes(b'running-image')
            result = self.install('fixed-a', selected)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'target={target} write={self.boot_a} backup={self.boot_b}', result.stdout)
            self.assertEqual(self.boot_a.read_bytes(), b'candidate-image')
            self.assertEqual(self.boot_b.read_bytes(), b'running-image')

    def test_fixed_a_refuses_to_back_up_an_uncommitted_running_image(self):
        self.boot_a.write_bytes(b'unknown-image')
        self.boot_b.write_bytes(b'committed-image')
        self.commit(b'committed-image')
        result = self.install('fixed-a')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ERROR:boot_store_not_committed', result.stderr)
        self.assertEqual(self.boot_a.read_bytes(), b'unknown-image')
        self.assertEqual(self.boot_b.read_bytes(), b'committed-image')

    def test_slotted_and_legacy_status_write_the_inactive_slot(self):
        for mode in ('slotted', ''):
            self.boot_a.write_bytes(b'running-image')
            self.boot_b.write_bytes(b'old-image')
            result = self.install(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'target=b write={self.boot_b} backup=', result.stdout)
            self.assertEqual(self.boot_a.read_bytes(), b'running-image')
            self.assertEqual(self.boot_b.read_bytes(), b'candidate-image')

    def test_unknown_store_mode_fails_closed(self):
        self.boot_a.write_bytes(b'running-image')
        self.boot_b.write_bytes(b'old-image')
        result = self.install('dual-x')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ERROR:boot_store_mode', result.stderr)
        self.assertEqual(self.boot_a.read_bytes(), b'running-image')


class FixedStoreTransactionTests(activation.ActivationTests):
    """Generation verification follows the store LK actually loads."""

    def set_bcb(self, slot, success, mode='fixed-a'):
        self.bcb.write_text(f'selected_slot={slot}\nslot_a_success=1\nslot_b_success={success}\n'
                            + (f'boot_store_mode={mode}\n' if mode else ''))

    def target_bytes(self):
        return b'boot-target'

    def test_fixed_a_candidate_is_verified_in_boot_a(self):
        self.prepared()
        # The candidate only in boot_b is exactly the issue #231 failure.
        self.assertEqual(self.boot.read_bytes(), self.target_bytes())
        result = self.verb('activate')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot-hash', result.stderr)
        self.boot_a.write_bytes(self.target_bytes())
        self.boot.write_bytes(b'boot-prior')
        result = self.verb('activate')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slotted_mode_still_verifies_boot_b(self):
        self.prepared()
        self.set_bcb('b', '0', mode='slotted')
        self.assertEqual(self.verb('activate').returncode, 0)
        self.set_bcb('b', '0', mode='')
        self.assertEqual(self.verb('activate').returncode, 0)

    def test_unknown_store_mode_fails_closed(self):
        self.prepared()
        self.set_bcb('b', '0', mode='dual-x')
        result = self.verb('activate')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot-store-mode', result.stderr)

    def committed(self):
        self.prepared()
        self.boot_a.write_bytes(self.target_bytes())
        self.set_bcb('b', '1')
        result = self.verb('commit')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.control / 'pending').exists())

    def test_restore_converges_boot_a_onto_committed_image(self):
        self.committed()
        self.boot_a.write_bytes(b'rolled-back-candidate')
        self.boot.write_bytes(self.target_bytes())
        result = self.verb('restore-boot-store')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BOOT_STORE_RESTORED', result.stdout)
        self.assertEqual(self.boot_a.read_bytes(), self.target_bytes())
        # Converged: a second boot is a no-op, so the reboot cannot loop.
        result = self.verb('restore-boot-store')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('BOOT_STORE_RESTORED', result.stdout)
        self.assertEqual(self.verb('activate-committed').returncode, 0)

    def test_restore_refuses_when_no_store_holds_committed_image(self):
        self.committed()
        self.boot_a.write_bytes(b'unknown-a')
        self.boot.write_bytes(b'unknown-b')
        result = self.verb('restore-boot-store')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot-store-unrecoverable', result.stderr)
        self.assertEqual(self.boot_a.read_bytes(), b'unknown-a')

    def test_restore_never_touches_a_pending_candidate_or_slotted_store(self):
        self.prepared()
        self.boot_a.write_bytes(b'candidate-under-test')
        result = self.verb('restore-boot-store')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.boot_a.read_bytes(), b'candidate-under-test')
        self.committed()
        self.set_bcb('b', '1', mode='slotted')
        self.boot_a.write_bytes(b'other-slot')
        result = self.verb('restore-boot-store')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('BOOT_STORE_RESTORED', result.stdout)
        self.assertEqual(self.boot_a.read_bytes(), b'other-slot')


class BootPathWiringTests(unittest.TestCase):
    def test_init_restores_before_committed_activation_and_reboots_once(self):
        init = (TOOLS / 'initramfs/libreecho-init').read_text()
        body = init[init.index('activate_feature_transaction()\n{'):]
        self.assertLess(body.index('restore_committed_boot_store "$transaction"'),
                        body.index('"$transaction" activate-committed'))
        helper = init[init.index('restore_committed_boot_store()'):init.index('activate_feature_transaction()\n{')]
        self.assertIn('restore-boot-store', helper)
        self.assertIn('*BOOT_STORE_RESTORED*', helper)
        self.assertIn('reboot -f', helper)


def load_tests(loader, standard_tests, pattern):
    # Run only the tests this module defines: the transaction fixture subclasses
    # ActivationTests for its setUp, and its inherited tests already run there.
    suite = unittest.TestSuite()
    for case in (BootctlStoreModeTests, UpdaterStoreSelectionTests,
                 FixedStoreTransactionTests, BootPathWiringTests):
        for name in sorted(vars(case)):
            if name.startswith('test_'):
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
