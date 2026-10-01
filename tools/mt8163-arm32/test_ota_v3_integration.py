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

    def test_normal_inspect_accepts_signed_v3(self):
        result = self.inspect(self.text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UPDATE_VALID version=0.14.0', result.stdout)

    def test_normal_inspect_refuses_v1_and_v2(self):
        for fmt in ('libreecho-ota-v1', 'libreecho-ota-v2'):
            result = self.inspect(self.text.replace('libreecho-ota-v3', fmt))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('legacy_manifest_unsupported', result.stderr)

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
