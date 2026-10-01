"""A rejected feature transaction cannot remove the web/update control plane."""
import subprocess
import test_ota_v3_manifest as grammar

TOOLS = grammar.TOOLS


class FailurePolicyTests(grammar.SignedFixture):
    def test_init_v3_uses_generation_verbs_without_local_adoption(self):
        source = (TOOLS / 'initramfs/libreecho-init').read_text()
        a = source.index('activate_feature_transaction()')
        b = source.index('\n}\n', a) + 3
        body = source[a:b]
        update = self.root / 'update'
        update.mkdir()
        (update / 'current').write_text('prior\n')
        transaction = self.root / 'transaction'
        transaction.write_text('#!/bin/sh\necho "$1" >> "$CALLS"\n')
        transaction.chmod(0o755)
        bootctl = self.root / 'bootctl'
        bootctl.write_text('#!/bin/sh\nprintf "selected_slot=%s\\nslot_b_success=%s\\n" "$SELECTED" "$SUCCESS"\n')
        bootctl.chmod(0o755)
        body = body.replace('/usr/local/sbin/libreecho-feature-transaction', str(transaction)).replace('/usr/local/sbin/libreecho-bootctl', str(bootctl)).replace('/data/libreecho/update', str(update))
        for selected, success, expected in (('b', '0', ['activate']), ('b', '1', ['commit', 'activate-committed']), ('a', '1', ['rollback', 'activate-committed'])):
            with self.subTest(selected=selected, success=success):
                calls = self.root / 'calls'
                if calls.exists(): calls.unlink()
                (update / 'pending').write_text('schema=3\nslot=b\ntransaction_id=test-target\nmanifest_sha256=' + 'a' * 64 + '\n')
                script = self.root / 'init-activate'
                script.write_text('BB=/bin/busybox\nlog() { :; }\npmsg_marker() { :; }\npersist_failure_log() { :; }\ncommit_fresh_install() { :; }\nadopt_staged_local_install() { echo adoption >> "$CALLS"; }\nrunning_slot_id() { echo "$SELECTED"; }\n' + body + '\nactivate_feature_transaction\n')
                result = subprocess.run(['/bin/busybox', 'sh', str(script)], env=dict(self.env, CALLS=str(calls), SELECTED=selected, SUCCESS=success), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls.read_text().splitlines(), expected)

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
