"""Recovery finalization and OTA assembly publish byte-identical target trees."""
import hashlib
import importlib.util
import shutil
import tarfile
from pathlib import Path
import test_ota_v3_convergence as convergence
import test_ota_v3_manifest as grammar

spec = importlib.util.spec_from_file_location('direct_install_tests', grammar.TOOLS / 'recovery-install/tests/test_direct_userdata_install.py')
direct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(direct)


class RecoveryParityTests(grammar.SignedFixture):
    def test_recovery_finalize_matches_ota_generation(self):
        ota = convergence.ConvergenceTests('test_empty')
        ota.setUp()
        self.addCleanup(ota.doCleanups)
        boot = b'ANDROID!' + bytes(direct.BOOT_BYTES - 8)
        ota.text = ota.text.replace('boot_sha256=' + 'a' * 64, 'boot_sha256=' + hashlib.sha256(boot).hexdigest())
        ota.p, ota.s = ota.signed(ota.text)
        result = ota.run_assembly()
        self.assertEqual(result.returncode, 0, result.stderr)
        h = direct.Harness(self.root / 'recovery')
        boot_file = h.add_file(h.assets, 'boot.img', boot)
        target_file = h.add_file(h.assets, 'manifest', ota.p.read_bytes())
        signature = h.add_file(h.assets, 'manifest.sig', ota.s.read_bytes())
        package = h.assets / 'local.tar'
        with tarfile.open(package, 'w') as tar:
            for p, name in ((boot_file, 'boot.img'), (target_file, 'manifest'), (signature, 'manifest.sig')):
                tar.add(p, arcname=name)
        fields = dict(row.split('=', 1) for row in ota.text.splitlines())
        staging = []
        for f in grammar.FEATURES:
            payload = h.add_file(h.assets, fields[f'feature_{f}_asset'], ota.assets[f][0])
            metadata = h.add_file(h.assets, fields[f'feature_{f}_manifest_asset'], ota.assets[f][1])
            staging.append((f, payload, metadata))
        transfer = {'boot': boot_file, 'ota-manifest': target_file, 'ota-signature': signature, 'local-package': package}
        files = [*transfer.values(), *(p for _, p, _ in staging), *(m for _, _, m in staging)]
        h.transfer_bytes = sum(p.stat().st_size for p in files)
        bundle = h.make_bundle_manifest(boot=boot_file, transfer=transfer, staging=staging)
        for phase in ('prepare', 'initialize', 'transfer'):
            result = h.run(phase, bundle)
            self.assertEqual(result.returncode, 0, result.stderr + str(h.receipt()))
        for p in files:
            shutil.copyfile(p, h.incoming / p.name)
        result = h.run('finalize', bundle, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + str(h.receipt()))
        self.assertFalse((h.data / 'libreecho/generations/test-target').exists())
        result = h.run('finalize', bundle)
        self.assertEqual(result.returncode, 0, result.stderr + str(h.receipt()))
        installed = h.data / 'libreecho/generations/test-target'
        expected = ota.generations / 'test-target'
        self.assertTrue(installed.is_dir(), 'recovery still installs history-dependent feature paths')
        snapshot = lambda root: {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(snapshot(installed), snapshot(expected))
        self.assertEqual((h.data / 'libreecho/update/current').read_text(), 'test-target\n')
        for p in [installed, *installed.rglob('*')]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o500 if p.is_dir() else 0o400)
        for f, payload, _ in staging:
            self.assertEqual((installed / 'features' / f / 'payload.squashfs').stat().st_ino,
                             (h.incoming / payload.name).stat().st_ino)


    def test_twrp_installer_publishes_the_same_generation_as_ota(self):
        """The TWRP zip and the browser helper consume one bundle; both must
        publish the OTA-assembled generation byte for byte and point current
        at it, with no legacy feature tree."""
        import subprocess, sys
        sys.path.insert(0, str(grammar.TOOLS / 'recovery-install/tests'))
        from test_installer_boot_write import extract_function, INSTALLER
        ota = convergence.ConvergenceTests('test_empty')
        ota.setUp()
        self.addCleanup(ota.doCleanups)
        result = ota.run_assembly()
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.root / 'twrp-bundle'
        bundle.mkdir()
        (bundle / 'manifest').write_bytes(ota.p.read_bytes())
        (bundle / 'manifest.sig').write_bytes(ota.s.read_bytes())
        fields = dict(row.split('=', 1) for row in ota.text.splitlines())
        for f in grammar.FEATURES:
            # Build-tag names, as the release ships them: found by digest.
            (bundle / f'build-{f}.squashfs').write_bytes(ota.assets[f][0])
            (bundle / f'build-{f}.manifest.json').write_bytes(ota.assets[f][1])
        data = self.root / 'twrp-data'
        source = INSTALLER.read_text()
        functions = '\n'.join(extract_function(source, name) for name in (
            'index_feature_assets', 'lookup_digest', 'stage_features', 'stage_v3_generation'))
        harness = self.root / 'twrp.sh'
        harness.write_text(
            'set -u\n'
            f'BUNDLE_DIR="{bundle}"\nDRY_RUN=0\n'
            f'LIVE_UPDATE="{data}/libreecho/update"\nSTAGING="$LIVE_UPDATE/staging"\n'
            f'LIVE_FEATURES="{data}/libreecho/features"\nLIVE_GENERATIONS="{data}/libreecho/generations"\n'
            'ui_print() { :; }\nreceipt_set() { :; }\n'
            'die() { printf "FAILED: %s\\n" "$*" >&2; exit 1; }\n'
            'sha256_of() { sha256sum "$1" | cut -d" " -f1; }\n'
            + functions + '\nstage_features\n')
        result = subprocess.run(['/bin/busybox', 'sh', str(harness)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = data / 'libreecho/generations' / fields['transaction_id']
        snapshot = lambda root: {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(snapshot(installed), snapshot(ota.generations / fields['transaction_id']))
        self.assertEqual((data / 'libreecho/update/current').read_text(), fields['transaction_id'] + '\n')
        for p in [installed, *installed.rglob('*')]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o500 if p.is_dir() else 0o400)
        self.assertFalse((data / 'libreecho/features').exists(), 'v3 must not populate the legacy feature tree')
        self.assertFalse((data / 'libreecho/update/staging/features').exists())
        # A second run must refuse rather than overwrite a published generation.
        result = subprocess.run(['/bin/busybox', 'sh', str(harness)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)

if __name__ == '__main__':
    import unittest
    unittest.main()
