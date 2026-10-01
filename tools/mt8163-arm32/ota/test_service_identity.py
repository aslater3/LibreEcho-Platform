"""Real kernel proc executable links and shipped strict identity helpers."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'initramfs/libreecho-generation-transaction'

class ServiceIdentity(unittest.TestCase):
    def probe(self, engine=True, corrupt=False, controller_bad=False, wrong_path=False, feature='airplay2'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run, var = root / 'run', root / 'var'
            var.mkdir(); (run / 'libreecho').mkdir(parents=True)
            controller = root / 'libreecho-airplayd'
            shutil.copyfile('/bin/busybox', controller); controller.chmod(0o755)
            name = 'libreecho-audio-engine' if feature == 'airplay2' else 'libreecho-ttsd'
            binary = run / f'libreecho/features/{feature}/root/usr/local/sbin/{name}'
            binary.parent.mkdir(parents=True)
            shutil.copyfile('/bin/busybox', binary); binary.chmod(0o755)
            expected = hashlib.sha256(binary.read_bytes()).hexdigest()
            processes = []
            def child(path):
                p = subprocess.Popen(['busybox', 'sleep', '60'], executable=str(path))
                processes.append(p)
                return p
            try:
                path = controller if feature == 'airplay2' else binary
                if controller_bad or wrong_path:
                    path = root / 'unrelated'
                    shutil.copyfile('/bin/busybox', path); path.chmod(0o755)
                owner = child(path)
                if feature == 'airplay2' and engine:
                    engine_path = binary
                    if wrong_path:
                        engine_path = root / 'unrelated-engine'
                        shutil.copyfile('/bin/busybox', engine_path); engine_path.chmod(0o755)
                    child(engine_path)
                service = 'airplayd' if feature == 'airplay2' else 'ttsd'
                (var / f'libreecho-{service}.pid').write_text(f'{owner.pid}\n')
                if corrupt: expected = '0' * 64
                manifest = root / 'manifest'
                manifest.write_text(f'feature_{feature}_daemon_sha256={expected}\nfeature_{feature}_daemon_path=usr/local/sbin/{name}\n')
                sockpath = run / 'libreecho' / ('airplay.sock' if feature == 'airplay2' else 'tts.sock')
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.bind(str(sockpath))
                    text = SOURCE.read_text().split('# Observation commands')[0]
                    text = text.replace('/usr/local/sbin/libreecho-airplayd', str(controller))
                    for key, val in dict(PROC_ROOT='/proc', RUN_ROOT=run, VAR_RUN_ROOT=var, PROC_NET_UNIX='/proc/net/unix', MANIFEST=manifest).items():
                        text += f'\n{key}={shlex.quote(str(val))}\n'
                    return subprocess.run(['/bin/busybox', 'sh'], input=text + f'\nservice_probe {feature}\n', text=True, capture_output=True)
            finally:
                for p in processes: p.terminate(); p.wait()

    def test_distinct_controller_and_signed_engine_pass(self):
        r = self.probe(); self.assertEqual(r.returncode, 0, r.stderr)
    def test_missing_engine_rejected(self):
        self.assertNotEqual(self.probe(engine=False).returncode, 0)
    def test_changed_engine_rejected(self):
        self.assertNotEqual(self.probe(corrupt=True).returncode, 0)
    def test_changed_controller_rejected(self):
        self.assertNotEqual(self.probe(controller_bad=True).returncode, 0)
    def test_matching_bytes_outside_candidate_mount_rejected(self):
        self.assertNotEqual(self.probe(wrong_path=True).returncode, 0)
    def test_other_services_still_require_signed_daemon(self):
        r = self.probe(feature='tts'); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.probe(feature='tts', wrong_path=True).returncode, 0)
        self.assertNotEqual(self.probe(feature='tts', corrupt=True).returncode, 0)

if __name__ == '__main__': unittest.main()
