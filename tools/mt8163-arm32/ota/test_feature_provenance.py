#!/usr/bin/env python3
"""Read-only provenance of the complete signed current generation."""
import hashlib
import subprocess
import unittest
import test_ota_v3_convergence as convergence

class ProvenanceTests(convergence.ConvergenceTests):
    def prepare(self):
        result = self.run_assembly()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.control / 'current').write_text('test-target\n')
        return self.generations / 'test-target'

    def invoke(self):
        return subprocess.run(['/bin/busybox', 'sh', str(convergence.TOOLS / 'initramfs/libreecho-generation-transaction'), 'provenance'],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_signed_current_generation_reports_observed_and_target_hashes_without_writes(self):
        self.prepare()
        before = {str(p): p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('generation=test-target', result.stdout)
        self.assertIn('target_manifest_sha256=' + hashlib.sha256(self.p.read_bytes()).hexdigest(), result.stdout)
        for f, (payload, metadata) in self.assets.items():
            self.assertIn('feature_' + f + '_payload_sha256=' + hashlib.sha256(payload).hexdigest(), result.stdout)
            self.assertIn('feature_' + f + '_target_manifest_sha256=' + hashlib.sha256(metadata).hexdigest(), result.stdout)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.data.rglob('*') if p.is_file()})
        self.assertFalse((self.control / 'generation.lock').exists())

    def test_payload_and_manifest_mismatch_are_stable_and_have_no_success_stdout(self):
        directory = self.prepare()
        for f in self.assets:
            for kind, filename, content in [('payload', 'payload.squashfs', self.assets[f][0]), ('manifest', 'manifest.json', self.assets[f][1])]:
                with self.subTest(feature=f, kind=kind):
                    path = directory / 'features' / f / filename
                    path.chmod(0o600)
                    path.write_bytes(b'stale')
                    result = self.invoke()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('provenance-' + f + '-' + kind + '-mismatch', result.stderr)
                    self.assertEqual(result.stdout, '')
                    path.write_bytes(content)
                    path.chmod(0o400)

    def test_pointer_identity_signature_and_incomplete_state_fail_closed(self):
        directory = self.prepare()
        for filename, bad, reason in [('target.manifest.sig', b'0' * 128 + b'\n', 'provenance-target-invalid'),
                                     ('COMPLETE', b'0' * 64 + b'\n', 'provenance-complete-mismatch')]:
            path = directory / filename
            original = path.read_bytes()
            path.chmod(0o600); path.write_bytes(bad)
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(reason, result.stderr)
            self.assertEqual(result.stdout, '')
            path.write_bytes(original); path.chmod(0o400)
        (self.control / 'current').write_text('../elsewhere\n')
        self.assertNotEqual(self.invoke().returncode, 0)
        (self.control / 'current').write_text('test-target\n')
        (self.control / 'pending').write_text('unconfirmed\n')
        self.assertNotEqual(self.invoke().returncode, 0)

if __name__ == '__main__':
    unittest.main()
