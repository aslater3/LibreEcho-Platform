"""Exercise the shipped service probe with isolated proc/socket fixtures."""
import hashlib
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'initramfs/libreecho-feature-transaction'

class ServiceIdentity(unittest.TestCase):
    def probe(self, engine=True, corrupt=False, controller_bad=False, wrong_path=False, feature='airplay2'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proc, run, var, staging = [root / x for x in ('proc', 'run', 'var', 'staging')]
            for p in (proc, run / 'libreecho', var, staging): p.mkdir(parents=True)
            controller = root / 'libreecho-airplayd'
            controller.write_bytes(b'boot controller')
            binary = run / 'libreecho/features/airplay2/root/usr/local/sbin/libreecho-audio-engine'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'engine')
            service = 'airplayd' if feature == 'airplay2' else 'ttsd'
            (var / f'libreecho-{service}.pid').write_text('10\n')
            (proc / '10').mkdir()
            actual_controller = root / 'bad-controller'
            actual_controller.write_bytes(b'wrong')
            (proc / '10/exe').symlink_to(actual_controller if controller_bad else controller)
            if engine:
                (proc / '11').mkdir()
                target = binary
                if wrong_path:
                    target = root / 'unrelated-engine'
                    target.write_bytes(b'engine')
                (proc / '11/exe').symlink_to(target)
            expected = hashlib.sha256(b'engine').hexdigest()
            if corrupt: binary.write_bytes(b'corrupt')
            (staging / 'manifest').write_text(f'feature_{feature}_daemon_sha256={expected}\n')
            sockpath = run / 'libreecho' / ('airplay.sock' if feature == 'airplay2' else 'tts.sock')
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(sockpath))
                net = root / 'unix'
                net.write_text(f'0: 0 0 0 0 0 0 {sockpath}\n')
                text = SOURCE.read_text()
                body = text[text.index('service_probe() {'):text.index('verify_required_services() {')]
                body = body.replace('/usr/local/sbin/libreecho-airplayd', str(controller))
                script = 'BB=/bin/busybox\n'
                for key, val in dict(PROC_ROOT=proc, RUN_ROOT=run, VAR_RUN_ROOT=var, STAGING=staging, PROC_NET_UNIX=net).items():
                    script += f'{key}={shlex.quote(str(val))}\n'
                script += '''fail() { echo "ERROR:$1" >&2; exit 1; }
regular() { [ -f "$1" ] && [ ! -L "$1" ] || fail "$2"; }
value() { $BB sed -n "s/^$1=//p" "$2"; }
file_hash() { $BB sha256sum "$1" | $BB cut -d ' ' -f 1; }
'''
                result = subprocess.run(['/bin/busybox', 'sh'], input=script + body + f'\nservice_probe {feature}\n', text=True, capture_output=True)
                return result

    def test_distinct_controller_and_signed_engine_pass(self):
        r = self.probe()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_engine_rejected(self):
        self.assertNotEqual(self.probe(engine=False).returncode, 0)

    def test_changed_engine_rejected(self):
        self.assertNotEqual(self.probe(corrupt=True).returncode, 0)

    def test_changed_controller_rejected(self):
        self.assertNotEqual(self.probe(controller_bad=True).returncode, 0)

    def test_matching_bytes_outside_candidate_mount_rejected(self):
        self.assertNotEqual(self.probe(wrong_path=True).returncode, 0)

    def test_other_services_still_require_signed_daemon(self):
        self.assertNotEqual(self.probe(feature='tts').returncode, 0)

if __name__ == '__main__':
    unittest.main()
