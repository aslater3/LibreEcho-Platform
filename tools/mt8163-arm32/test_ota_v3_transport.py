"""v3 discovery and TLS bootstrap cannot depend on a legacy feature tree."""
import subprocess
import test_ota_v3_manifest as grammar


class TransportTests(grammar.SignedFixture):
    def test_v3_discovery_uses_separate_pointer(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        self.assertIn('/release-pointer-v3.txt', source)
        self.assertNotIn('/release-pointer.txt', source)

    def test_https_bootstrap_authenticates_current_generation_without_legacy_writes(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        a = source.index('prepare_https_client()')
        b = source.index('\n}\n', a) + 3
        body = source[a:b]
        self.assertIn('GENERATION_TOOL', body)
        self.assertIn('current', body)
        self.assertNotIn('chmod', body)
        self.assertNotIn('manifest_payload_value', body)

    def test_legacy_state_alone_cannot_bootstrap_tls(self):
        source = (grammar.TOOLS / 'initramfs/libreecho-update-fetch').read_text()
        a = source.index('prepare_https_client()')
        b = source.index('\n}\n', a) + 3
        control = self.root / 'update'
        control.mkdir()
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / 'payload.squashfs').write_bytes(b'legacy')
        (legacy / 'manifest.json').write_text('{}')
        script = self.root / 'tls'
        script.write_text('BB=/bin/busybox\nROOT=' + str(control) + '\nASSISTANT_PAYLOAD=' + str(legacy / 'payload.squashfs') + '\nASSISTANT_MANIFEST=' + str(legacy / 'manifest.json') + '\ndie() { echo "ERROR:$1" >&2; exit 1; }\n' + source[a:b] + '\nprepare_https_client\n')
        result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legacy_manifest_unsupported', result.stderr)
        self.assertEqual((legacy / 'payload.squashfs').read_bytes(), b'legacy')


if __name__ == '__main__':
    import unittest
    unittest.main()
