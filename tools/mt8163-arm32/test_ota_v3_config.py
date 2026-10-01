"""Versioned config is the only carried user state; unsupported schemas are inert."""
import subprocess
import test_ota_v3_manifest as grammar

MIGRATE = grammar.TOOLS / 'initramfs/libreecho-config-migrate'


class ConfigTests(grammar.SignedFixture):
    def run_config(self, text, target='1'):
        config = self.root / 'config'
        config.mkdir(exist_ok=True)
        path = config / 'web-config.json'
        path.write_text(text)
        result = subprocess.run(['/bin/busybox', 'sh', str(MIGRATE), target],
                                env=dict(self.env, CONFIG_ROOT=str(config), ROOT=str(self.root / 'update')),
                                capture_output=True, text=True)
        self.assertEqual(path.read_text(), text)
        return result

    def test_current_schema_is_idempotent_and_byte_unchanged(self):
        result = self.run_config('{"schema":1,"hostname":"user","nested":{"schema":99}}\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('config_error=\n', (self.root / 'update/config-status').read_text())
        self.assertEqual(sorted(p.name for p in (self.root / 'config').iterdir()), ['web-config.json'])

    def test_unknown_newer_or_absent_schema_is_untouched_with_banner_flag(self):
        for text in ('{"schema":99,"secret":"keep"}', '{"hostname":"keep"}', '{"schema":"1"}', '{"schema":0}'):
            with self.subTest(text=text):
                result = self.run_config(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('config_error=unsupported_schema', (self.root / 'update/config-status').read_text())
                self.assertFalse((self.root / 'update/config-status.tmp').exists())

    def test_no_invented_migration_to_an_unknown_target(self):
        result = self.run_config('{"schema":1,"secret":"keep"}', '2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('config_error=unsupported_target_schema', (self.root / 'update/config-status').read_text())

    def test_nested_strings_duplicate_or_invalid_json_cannot_forge_root_schema(self):
        for text in ('{"nested":{"schema":1}}', '{"schema":1,"schema":99}',
                     '{"schema":1} trailing', '{"schema":1,}', '{"schema":1,"x":"bad\\q"}',
                     '{"schema":1,"x": [}', '{"schema":1.0}', '{"schema":01}'):
            with self.subTest(text=text):
                result = self.run_config(text)
                self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    import unittest
    unittest.main()
