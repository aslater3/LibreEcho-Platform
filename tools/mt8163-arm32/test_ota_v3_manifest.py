"""Signed target grammar, exercised with BusyBox and real Ed25519 signatures."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from nacl.signing import SigningKey

TOOLS = Path(__file__).resolve().parent
PARSER = TOOLS / 'initramfs/libreecho-target-manifest'
FEATURES = ('airplay2', 'tts', 'wakeword', 'stt', 'assistant')
DAEMONS = ('libreecho-audio-engine', 'libreecho-ttsd', 'libreecho-waked', 'libreecho-sttd', 'libreecho-agentd')


def manifest(assets=None, tx='test-target'):
    assets = assets or {f: (('payload:' + f).encode(), ('manifest:' + f).encode()) for f in FEATURES}
    rows = [('format', 'libreecho-ota-v3'), ('manifest_version', '1'),
            ('board', 'radar_puffin'), ('soc', 'mt8163'), ('architecture', 'armv7'),
            ('image_profile', 'ota'), ('transaction_type', 'system'), ('transaction_id', tx),
            ('release', 'radar-puffin-v0.14.0-test'), ('version', '0.14.0'),
            ('update_channel', 'dev'), ('service_profile', 'production'),
            ('minimum_updater_schema', '3'), ('commit_policy', 'after-slot-confirm'),
            ('boot_filename', 'boot.img'), ('boot_size', '16777216'),
            ('boot_sha256', 'a' * 64), ('feature_ids', ','.join(FEATURES))]
    for f, daemon in zip(FEATURES, DAEMONS):
        p, m = assets[f]
        ph, mh = hashlib.sha256(p).hexdigest(), hashlib.sha256(m).hexdigest()
        rows.extend((('feature_' + f + '_' + k, str(v)) for k, v in (
            ('asset', f'libreecho-radar-puffin-base-{f}-{ph}.payload.squashfs'),
            ('size', len(p)), ('sha256', ph),
            ('manifest_asset', f'libreecho-radar-puffin-base-{f}-{mh}.manifest.json'),
            ('manifest_size', len(m)), ('manifest_sha256', mh),
            ('daemon_path', 'usr/local/sbin/' + daemon), ('daemon_sha256', 'b' * 64))))
    rows.append(('config_schema', '1'))
    return ''.join(f'{k}={v}\n' for k, v in rows)


class SignedFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = SigningKey.generate()
        (self.root / 'key').write_text(self.key.verify_key.encode().hex())
        (self.root / 'target').write_text('target_id=radar_puffin\n')
        verifier = self.root / 'verify'
        verifier.write_text('#!/usr/bin/env python3\nimport sys\nfrom nacl.signing import VerifyKey\nfrom pathlib import Path\nVerifyKey(bytes.fromhex(Path(sys.argv[1]).read_text().strip())).verify(Path(sys.argv[2]).read_bytes(), bytes.fromhex(Path(sys.argv[3]).read_text().strip()))\n')
        verifier.chmod(0o755)
        self.env = dict(os.environ, TARGET_FILE=str(self.root / 'target'),
                        VERIFY=str(verifier), PUBLIC_KEY=str(self.root / 'key'))

    def signed(self, text):
        p = self.root / 'target.manifest'
        p.write_text(text)
        s = self.root / 'target.manifest.sig'
        s.write_text(self.key.sign(p.read_bytes()).signature.hex() + '\n')
        return p, s

    def check(self, text):
        p, s = self.signed(text)
        return subprocess.run(['/bin/busybox', 'sh', str(PARSER), 'check', str(p), str(s)],
                              env=self.env, text=True, capture_output=True)


class ManifestTests(SignedFixture):
    def test_accepts_canonical_v3(self):
        result = self.check(manifest())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_forbidden_keys(self):
        for key in ('feature_wakeword_action', 'feature_wakeword_base_payload_sha256',
                    'feature_policy', 'runtime_asset', 'feature_tts_activation'):
            with self.subTest(key=key):
                self.assertNotEqual(self.check(manifest() + key + '=bad\n').returncode, 0)

    def test_rejects_missing_asset_fields_for_any_feature(self):
        for f in FEATURES:
            for field in ('asset', 'size', 'sha256', 'manifest_asset', 'manifest_size', 'manifest_sha256'):
                with self.subTest(feature=f, field=field):
                    text = ''.join(line + '\n' for line in manifest().splitlines()
                                   if not line.startswith('feature_' + f + '_' + field + '='))
                    self.assertNotEqual(self.check(text).returncode, 0)

    def test_rejects_noncontent_addressed_name(self):
        self.assertNotEqual(self.check(manifest().replace('base-airplay2-', 'other-airplay2-')).returncode, 0)

    def test_rejects_board_mismatch(self):
        self.assertNotEqual(self.check(manifest().replace('board=radar_puffin', 'board=biscuit')).returncode, 0)

    def test_rejects_v2_format(self):
        result = self.check(manifest().replace('libreecho-ota-v3', 'libreecho-ota-v2'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legacy_manifest_unsupported', result.stderr)

    def test_rejects_wrong_signature(self):
        p, s = self.signed(manifest())
        p.write_text(p.read_text().replace('version=0.14.0', 'version=0.14.1'))
        result = subprocess.run(['/bin/busybox', 'sh', str(PARSER), 'check', str(p), str(s)], env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_duplicates_reordering_and_unsafe_transaction(self):
        text = manifest()
        for bad in (text + 'config_schema=1\n', '\n'.join(reversed(text.splitlines())) + '\n',
                    text.replace('transaction_id=test-target', 'transaction_id=../escape'),
                    text.replace('config_schema=1', 'config_schema=1x'), text.rstrip('\n')):
            self.assertNotEqual(self.check(bad).returncode, 0)


if __name__ == '__main__':
    unittest.main()
