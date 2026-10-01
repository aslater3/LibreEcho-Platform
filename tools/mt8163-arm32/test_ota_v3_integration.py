"""The normal updater/fetch path must use v3, not only the assembly verb."""
import hashlib
import subprocess
import tarfile
from pathlib import Path
import test_ota_v3_manifest as grammar

TOOLS = grammar.TOOLS


class UpdaterTests(grammar.SignedFixture):
    def setUp(self):
        super().setUp()
        self.data = self.root / 'data'
        self.update = self.data / 'libreecho/update'
        self.stage = self.update / 'staging'
        self.stage.mkdir(parents=True)
        self.boot = b'ANDROID!' + b'\x00' * (16777216 - 8)
        self.text = grammar.manifest().replace('boot_sha256=' + 'a' * 64,
                                             'boot_sha256=' + hashlib.sha256(self.boot).hexdigest())
        self.env['TARGET_MANIFEST'] = str(TOOLS / 'initramfs/libreecho-target-manifest')

    def script(self, entry):
        source = (TOOLS / 'initramfs/libreecho-update').read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace('DATA_ROOT=/data', 'DATA_ROOT=' + str(self.data))
        source = source.replace('VERIFY=/usr/local/libexec/libreecho-update-verify', 'VERIFY=' + self.env['VERIFY'])
        source = source.replace('PUBLIC_KEY=/etc/libreecho/ota-public-key.hex', 'PUBLIC_KEY=' + self.env['PUBLIC_KEY'])
        source += '\nrequire_userdata() { mkdir -p "$UPDATE_ROOT"; }\nlock_take() { :; }\nchannel_value() { echo dev; }\n' + entry + '\n'
        script = self.root / 'updater'
        script.write_text(source)
        return script

    def inspect(self, text):
        p, sig = self.signed(text)
        (self.root / 'boot.img').write_bytes(self.boot)
        package = self.root / 'package.tar'
        with tarfile.open(package, 'w') as tar:
            for path, name in ((p, 'manifest'), (sig, 'manifest.sig'), (self.root / 'boot.img', 'boot.img')):
                tar.add(path, arcname=name)
        script = self.script('inspect_package "$1"')
        return subprocess.run(['/bin/busybox', 'sh', str(script), str(package)], env=self.env,
                              capture_output=True, text=True)

    def test_channel_mismatch_is_refused(self):
        result = self.inspect(self.text.replace('update_channel=dev', 'update_channel=stable'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('update_channel_mismatch', result.stderr)

    def test_legacy_machinery_is_physically_absent(self):
        for name in ('libreecho-feature-transaction', 'libreecho-update'):
            source = (TOOLS / 'initramfs' / name).read_text()
            for forbidden in ('preserved_identity', 'reconcile_operation', 'move_expected', 'abort_operation_untouched',
                              'runtime_authority', 'base_payload_sha256', 'build_intent', 'verify_preserved_feature_identity'):
                self.assertNotIn(forbidden, source)

    def test_status_reads_current_generation_not_legacy_features(self):
        directory = self.data / 'libreecho/generations/test-target/features'
        for f in grammar.FEATURES:
            d = directory / f
            d.mkdir(parents=True)
            (d / 'payload.squashfs').write_bytes(b'current')
            (d / 'manifest.json').write_bytes(b'metadata')
            old = self.data / 'libreecho/features' / f
            old.mkdir(parents=True)
            (old / 'payload.squashfs').write_bytes(b'legacy')
        self.control_file('current').write_text('test-target\n')
        script = self.script('feature_status')
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('feature_airplay2_payload_sha256=' + hashlib.sha256(b'current').hexdigest(), result.stdout)
        self.assertNotIn(hashlib.sha256(b'legacy').hexdigest(), result.stdout)
        self.control_file('current').write_text('../features\n')
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(hashlib.sha256(b'legacy').hexdigest(), result.stdout)

    def test_normal_inspect_accepts_signed_v3(self):
        result = self.inspect(self.text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UPDATE_VALID version=0.14.0', result.stdout)

    def test_legacy_feature_transaction_refuses_v2(self):
        source = (TOOLS / 'initramfs/libreecho-feature-transaction').read_text().split('# V3 state never enters', 1)[0]
        source = source.replace('VERIFY=/usr/local/libexec/libreecho-update-verify', 'VERIFY=' + self.env['VERIFY']).replace('PUBLIC_KEY=/etc/libreecho/ota-public-key.hex', 'PUBLIC_KEY=' + self.env['PUBLIC_KEY'])
        script = self.root / 'legacy-check'
        script.write_text(source + '\ncheck_manifest "$1" "$2"\n')
        p, sig = self.signed(self.text.replace('libreecho-ota-v3', 'libreecho-ota-v2'))
        result = subprocess.run(['/bin/busybox', 'sh', str(script), str(p), str(sig)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legacy_manifest_unsupported', result.stderr)

    def test_normal_inspect_refuses_v1_and_v2(self):
        for fmt in ('libreecho-ota-v1', 'libreecho-ota-v2'):
            result = self.inspect(self.text.replace('libreecho-ota-v3', fmt))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('legacy_manifest_unsupported', result.stderr)

    def assembled_package(self):
        p, sig = self.signed(self.text)
        cache = self.update / 'asset-cache'; cache.mkdir(exist_ok=True)
        for feature in grammar.FEATURES:
            for content in (('payload:' + feature).encode(), ('manifest:' + feature).encode()):
                (cache / (hashlib.sha256(content).hexdigest() + '.part')).write_bytes(content)
        generations = self.data / 'libreecho/generations'
        self.env.update(ROOT=str(self.update), GENERATIONS=str(generations),
                        GENERATION_TOOL=str(TOOLS / 'initramfs/libreecho-generation'), SPACE_RESERVE_BYTES='0')
        result = subprocess.run(['/bin/busybox', 'sh', self.env['GENERATION_TOOL'], 'assemble', str(p), str(sig)], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((generations / 'test-target.pin').exists())
        (self.root / 'boot.img').write_bytes(self.boot)
        package = self.root / 'package.tar'
        with tarfile.open(package, 'w') as tar:
            for path, name in ((p, 'manifest'), (sig, 'manifest.sig'), (self.root / 'boot.img', 'boot.img')): tar.add(path, arcname=name)
        return package, generations

    def test_early_install_failure_releases_assembled_pin_only(self):
        package, generations = self.assembled_package()
        other = generations / 'other-target.pin'; other.write_text('other-target\n')
        bootctl = self.root / 'failing-bootctl'
        bootctl.write_text('#!/bin/sh\nexit 1\n'); bootctl.chmod(0o755)
        script = self.script('install_package "$1"')
        script.write_text(script.read_text().replace('BOOTCTL=/usr/local/sbin/libreecho-bootctl', 'BOOTCTL=' + str(bootctl)))
        result = subprocess.run(['/bin/busybox', 'sh', str(script), str(package)], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('boot_control_status', result.stderr)
        self.assertFalse((generations / 'test-target.pin').exists())
        self.assertEqual(other.read_text(), 'other-target\n')
        self.assertFalse(self.control_file('pending').exists())

    def test_unverified_install_cannot_release_assembled_pin(self):
        package, generations = self.assembled_package()
        package.write_bytes(b'invalid package')
        script = self.script('tx=test-target\ninstall_package "$1"')
        result = subprocess.run(['/bin/busybox', 'sh', str(script), str(package)], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((generations / 'test-target.pin').exists())

    def test_install_records_v3_pending_and_confirms_generation(self):
        p, sig = self.signed(self.text)
        directory = self.data / 'libreecho/generations/test-target'
        directory.mkdir(parents=True)
        (directory.parent / 'test-target.pin').write_text('test-target\n')
        (directory / 'target.manifest').write_bytes(p.read_bytes())
        (directory / 'target.manifest.sig').write_bytes(sig.read_bytes())
        (directory / 'COMPLETE').write_text(hashlib.sha256(p.read_bytes()).hexdigest() + '\n')
        for f in grammar.FEATURES:
            feature = directory / 'features' / f
            feature.mkdir(parents=True)
            (feature / 'payload.squashfs').write_bytes(('payload:' + f).encode())
            (feature / 'manifest.json').write_bytes(('manifest:' + f).encode())
        (self.control_file('current')).write_text('prior\n')
        (self.root / 'boot.img').write_bytes(self.boot)
        package = self.root / 'package.tar'
        with tarfile.open(package, 'w') as tar:
            for path, name in ((p, 'manifest'), (sig, 'manifest.sig'), (self.root / 'boot.img', 'boot.img')):
                tar.add(path, arcname=name)
        boot_b = self.root / 'boot_b'
        boot_b.write_bytes(b'old')
        bootctl = self.root / 'bootctl'
        bootctl.write_text('#!/bin/sh\ncase "$1" in status) echo selected_slot=a; echo inactive_slot=b; echo slot_a_success=1;; activate) echo "$2" > "$ACTIVATED";; esac\n')
        bootctl.chmod(0o755)
        script = self.script('install_package "$1"')
        text = script.read_text().replace('BOOTCTL=/usr/local/sbin/libreecho-bootctl', 'BOOTCTL=' + str(bootctl))
        text = text.replace('install_package "$1"', 'target_device_for_slot() { target_device="' + str(boot_b) + '"; }\ninstall_package "$1"')
        script.write_text(text)
        env = dict(self.env, GENERATION_TOOL=str(TOOLS / 'initramfs/libreecho-generation'),
                   GENERATIONS=str(directory.parent), ROOT=str(self.update), ACTIVATED=str(self.root / 'activated'))
        result = subprocess.run(['/bin/busybox', 'sh', str(script), str(package)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(boot_b.read_bytes(), self.boot)
        expected = 'schema=3\nslot=b\ntransaction_id=test-target\nmanifest_sha256=' + hashlib.sha256(p.read_bytes()).hexdigest() + '\n'
        self.assertEqual(self.control_file('pending').read_text(), expected)
        self.assertEqual((self.root / 'activated').read_text(), 'b\n')
        self.assertFalse((directory.parent / 'test-target.pin').exists())

    def test_confirm_v3_calls_target_activation_then_slot_confirm_then_commit(self):
        self.control_file('pending').write_text('schema=3\nslot=b\ntransaction_id=test-target\nmanifest_sha256=' + 'a' * 64 + '\n')
        calls = self.root / 'calls'
        helper = self.root / 'transaction'
        helper.write_text('#!/bin/sh\necho "transaction:$1" >> "$CALLS"\n')
        helper.chmod(0o755)
        bootctl = self.root / 'bootctl'
        bootctl.write_text('#!/bin/sh\necho "bootctl:$1:$2" >> "$CALLS"\n')
        bootctl.chmod(0o755)
        script = self.script('target_device_for_slot() { :; }\nconfirm_pending')
        script.write_text(script.read_text().replace('FEATURE_TRANSACTION=/usr/local/sbin/libreecho-feature-transaction', 'FEATURE_TRANSACTION=' + str(helper)).replace('BOOTCTL=/usr/local/sbin/libreecho-bootctl', 'BOOTCTL=' + str(bootctl)))
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=dict(self.env, CALLS=str(calls)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ['transaction:verify-running', 'bootctl:confirm:b', 'transaction:commit'])

    def control_file(self, name):
        return self.update / name

    def test_fetch_download_path_calls_generation_builder(self):
        source = (TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        a = source.index('download_feature_assets()')
        b = source.index('\n}\n', a) + 3
        body = source[a:b]
        builder = self.root / 'builder'
        builder.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$CALLED"\n')
        builder.chmod(0o755)
        (self.stage / 'manifest').write_text(self.text)
        script = self.root / 'fetch-path'
        script.write_text('BB=/bin/busybox\nROOT=' + str(self.update) + '\nGENERATION_TOOL=' + str(builder)
                          + '\ncheck_value_from_file() { sed -n "s/^$2=//p" "$1"; }\ndie() { exit 1; }\n'
                          + body + '\ndownload_feature_assets\n')
        env = dict(self.env, CALLED=str(self.root / 'called'))
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'called').exists(), 'normal fetch silently skipped the v3 assets')
        self.assertIn('assemble', (self.root / 'called').read_text())


if __name__ == '__main__':
    import unittest
    unittest.main()
