"""History-independent generation assembly through the fetch command."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from test_ota_v3_manifest import FEATURES, TOOLS, SignedFixture, manifest

FETCH = TOOLS / 'initramfs/libreecho-update-fetch'
GENERATION = TOOLS / 'initramfs/libreecho-generation'


class ConvergenceTests(SignedFixture):
    def setUp(self):
        super().setUp()
        self.assets = {f: ((f + ':target-payload\n').encode() * 16,
                           json.dumps({'feature': f, 'version': 'target'}, sort_keys=True).encode()) for f in FEATURES}
        self.text = manifest(self.assets)
        self.p, self.s = self.signed(self.text)
        self.data = self.root / 'data/libreecho'
        self.generations = self.data / 'generations'
        self.control = self.data / 'update'
        self.control.mkdir(parents=True)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.requests = self.root / 'requests'
        fields = dict(line.split('=', 1) for line in self.text.splitlines())
        for f in FEATURES:
            for field, content in zip(('asset', 'manifest_asset'), self.assets[f]):
                (self.server / fields[f'feature_{f}_{field}']).write_bytes(content)
        curl = self.root / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args=sys.argv[1:]
out=Path(args[args.index('--output')+1])
content=(Path(os.environ['SERVER']) / args[-1].rsplit('/',1)[-1]).read_bytes()
with Path(os.environ['REQUESTS']).open('a') as log: log.write(args[-1].rsplit('/',1)[-1]+'\\n')
start=out.stat().st_size if '--continue-at' in args and out.exists() else 0
fail=Path(os.environ['FAIL_ONCE'])
if fail.exists():
    fail.unlink()
    with out.open('ab' if start else 'wb') as file: file.write(content[start:start+7])
    sys.exit(18)
with out.open('ab' if start else 'wb') as file: file.write(content[start:])
print('206' if start else '200', end='')
''')
        curl.chmod(0o755)
        self.env.update(ROOT=str(self.control), GENERATIONS=str(self.generations),
                        GENERATION_TOOL=str(GENERATION), TARGET_MANIFEST=str(TOOLS / 'initramfs/libreecho-target-manifest'),
                        CURL=str(curl), CA=str(self.root / 'ca'), SERVER=str(self.server),
                        REQUESTS=str(self.requests), FAIL_ONCE=str(self.root / 'fail-once'), SPACE_RESERVE_BYTES='0')

    def run_assembly(self):
        return subprocess.run(['/bin/busybox', 'sh', str(FETCH), 'assemble-generation',
                               str(self.p), str(self.s)], env=self.env, capture_output=True, text=True)

    def seed_current(self):
        current = self.generations / 'prior'
        for f in FEATURES:
            d = current / 'features' / f
            d.mkdir(parents=True)
            for name, data in zip(('payload.squashfs', 'manifest.json'), self.assets[f]):
                (d / name).write_bytes(data)
                (d / name).chmod(0o400)
            d.chmod(0o500)
        (self.control / 'current').write_text('prior\n')
        return current

    def assert_target(self, count):
        result = self.run_assembly()
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = self.generations / 'test-target'
        expected = {'target.manifest': self.p.read_bytes(), 'target.manifest.sig': self.s.read_bytes(),
                    'COMPLETE': (hashlib.sha256(self.p.read_bytes()).hexdigest() + '\n').encode()}
        for f in FEATURES:
            for name, data in zip(('payload.squashfs', 'manifest.json'), self.assets[f]):
                expected[f'features/{f}/{name}'] = data
        observed = {str(p.relative_to(dest)): p.read_bytes() for p in dest.rglob('*') if p.is_file()}
        self.assertEqual(observed, expected)
        for p in [dest, *dest.rglob('*')]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o500 if p.is_dir() else 0o400)
        self.assertEqual(len(self.requests.read_text().splitlines()) if self.requests.exists() else 0, count)
        self.assertFalse((self.generations / 'test-target.partial').exists())

    def test_empty(self):
        self.assert_target(10)

    def test_exact_current_zero_downloads(self):
        current = self.seed_current()
        self.assert_target(0)
        for f in FEATURES:
            self.assertEqual((current / 'features' / f / 'payload.squashfs').stat().st_ino,
                             (self.generations / 'test-target/features' / f / 'payload.squashfs').stat().st_ino)

    def test_radar_legacy_world_writable_state_is_ignored(self):
        hashes = {'wakeword': 'cebebeef', 'assistant': '19c44003', 'airplay2': '330d558f',
                  'stt': '6c1e866b', 'tts': 'a9defee5'}
        for f in FEATURES:
            d = self.data / 'features' / f
            d.mkdir(parents=True)
            d.chmod(0o777)
            for name in ('payload.squashfs', 'manifest.json'):
                (d / name).write_text('Radar captured identity prefix: ' + hashes[f])
                (d / name).chmod(0o666)
        self.assert_target(10)
        self.assertEqual((self.data / 'features/wakeword/payload.squashfs').stat().st_mode & 0o777, 0o666)

    def test_corrupted_current_downloads_only_mismatch(self):
        current = self.seed_current()
        p = current / 'features/wakeword/payload.squashfs'
        p.chmod(0o600)
        p.write_bytes(bytes([self.assets['wakeword'][0][0] ^ 1]) + self.assets['wakeword'][0][1:])
        p.chmod(0o400)
        self.assert_target(1)

    def test_stray_files_absent(self):
        current = self.seed_current()
        (current / 'features/wakeword').chmod(0o700)
        (current / 'features/wakeword/extra.bin').write_bytes(b'extra')
        (current / 'features/foo').mkdir()
        (current / 'runtime.squashfs').write_bytes(b'extra')
        (current / 'extra').write_bytes(b'extra')
        self.assert_target(0)

    def test_half_written_partial_discarded(self):
        partial = self.generations / 'test-target.partial/features/wakeword'
        partial.mkdir(parents=True)
        (partial / 'payload.squashfs').write_bytes(b'half')
        (partial / 'extra.bin').write_bytes(b'extra')
        self.assert_target(10)

    def test_interrupted_download_resumes(self):
        (self.root / 'fail-once').touch()
        first = self.run_assembly()
        self.assertNotEqual(first.returncode, 0)
        self.assertFalse((self.generations / 'test-target/COMPLETE').exists())
        self.assert_target(11)

    def test_completed_generation_is_idempotent(self):
        self.assert_target(10)
        self.assert_target(10)

    def test_symlink_current_cannot_redirect_reuse(self):
        self.generations.mkdir()
        (self.generations / 'prior').symlink_to(self.server, target_is_directory=True)
        (self.control / 'current').write_text('prior\n')
        result = self.run_assembly()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.requests.exists())


if __name__ == '__main__':
    import unittest
    unittest.main()
