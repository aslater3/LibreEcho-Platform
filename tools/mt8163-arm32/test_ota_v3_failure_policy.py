"""A rejected feature transaction cannot remove the web/update control plane."""
import subprocess
import test_ota_v3_manifest as grammar

TOOLS = grammar.TOOLS


class FailurePolicyTests(grammar.SignedFixture):
    def test_feature_failure_starts_web_but_not_feature_daemons(self):
        source = (TOOLS / 'initramfs/libreecho-init').read_text()
        a = source.index('start_ui_services()')
        b = source.index('\n}\n', a) + 3
        body = source[a:b]
        root = self.root
        for old, new in (('/usr/local/sbin', str(root / 'bin')), ('/etc/init.d', str(root / 'init.d')),
                         ('/data/libreecho', str(root / 'data/libreecho')), ('/var/run', str(root / 'var/run')),
                         ('/var/log', str(root / 'var/log')), ('/run/libreecho', str(root / 'run/libreecho')),
                         ('/tmp/', str(root) + '/')):
            body = body.replace(old, new)
        (root / 'bin').mkdir()
        (root / 'bin/libreecho-web').write_text('#!/bin/sh\nexit 0\n')
        (root / 'bin/libreecho-web').chmod(0o755)
        initd = root / 'init.d'
        initd.mkdir()
        for service in ('web', 'waked', 'ttsd', 'airplayd'):
            p = initd / ('libreecho-' + service + '.init')
            p.write_text('#!/bin/sh\necho ' + service + ' >> "$CALLS"\n')
            p.chmod(0o755)
        script = root / 'init'
        script.write_text('BB=/bin/busybox\nSERVICE_PROFILE=production\nFEATURE_POLICY=community-noncommercial\nDATA_CLEANUP_OK=1\n'
                          'log() { :; }\npmsg_marker() { :; }\napply_timezone() { :; }\n'
                          'activate_feature_transaction() { echo ERROR:feature-stt-payload-hash; return 1; }\n'
                          'stop_ui_voice_owners() { :; }\nstart_shared_discovery_runtime() { :; }\n'
                          'recovery_radio_ready_wait() { :; }\nstart_persisted_feature_services() { echo persisted >> "$CALLS"; }\n'
                          + body + '\nstart_ui_services\n')
        result = subprocess.run(['/bin/busybox', 'sh', str(script)],
                                env=dict(self.env, CALLS=str(root / 'calls')), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / 'calls').read_text().splitlines(), ['web'])
        state = root / 'data/libreecho/update/features-status'
        self.assertIn('features_state=degraded', state.read_text())
        self.assertIn('features_error=feature-stt-payload-hash', state.read_text())


if __name__ == '__main__':
    import unittest
    unittest.main()
