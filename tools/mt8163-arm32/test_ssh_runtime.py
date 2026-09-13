"""Host-isolated SSH and ADB control regressions; never touches a device."""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class SSHRuntimeTests(unittest.TestCase):
    def test_account_polls_delete_and_preserve_uids(self):
        source = (ROOT / 'ssh/libreecho-ssh.init').read_text()
        functions = source[source.index('users_file_ready()'):source.index('dropbear_running()')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = root / 'bb'
            wrapper.write_text('#!/bin/sh\ncase "$1" in\nchown) exit 0;;\nstat) if [ "$3" = %u ]; then printf "0\\n"; exit; fi;;\nesac\nexec "$@"\n')
            wrapper.chmod(0o755)
            for name in ['state', 'home']:
                (root / name).mkdir()
            users = root / 'users'
            users.touch(mode=0o600)
            script = functions + '\nwrite_static_accounts && sync_accounts\n'
            env = dict(os.environ, BB=str(wrapper), USERS_FILE=str(users),
                       PASSWD_FILE=str(root / 'passwd'), GROUP_FILE=str(root / 'group'),
                       SHELLS_FILE=str(root / 'shells'), HOME_ROOT=str(root / 'home'),
                       STATE_ROOT=str(root / 'state'))
            def poll(names):
                users.write_text(''.join(f'{n}:sha256:{"a" * 16}:{"b" * 64}\n' for n in names))
                result = subprocess.run(['sh', '-c', script], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                records = (root / 'passwd').read_text().splitlines()
                self.assertEqual([r.split(':')[0] for r in records], ['root'] + names)
                return (root / 'state/accounts').read_text()
            first = poll(['alice', 'bob'])
            self.assertEqual(first, poll(['alice', 'bob']))
            self.assertEqual(first, poll(['alice', 'bob']))
            remaining = poll(['bob', 'carol'])
            self.assertIn('bob:1001\n', remaining)
            self.assertIn('carol:1002\n', remaining)
            self.assertFalse((root / 'home/alice').exists())
            self.assertEqual(remaining, poll(['bob', 'carol']))
            restored = poll(['alice', 'bob'])
            self.assertEqual(first, restored)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_auth_hook_accepts_and_rejects_real_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            users = root / 'users'
            (root / 'includes.h').write_text('')
            source = (ROOT / 'ssh/libreecho-auth.c').read_text()
            # Redirect only filesystem identity and root ownership in the host fixture.
            source = source.replace('"/data/libreecho/config/users"', '"' + str(users) + '"')
            source = source.replace('st.st_uid != 0', 'st.st_uid != getuid()')
            (root / 'auth.c').write_text(source)
            subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror', '-I', tmp,
                            str(root / 'auth.c'), '-o', str(root / 'auth.so')], check=True, timeout=30, capture_output=True)
            auth = ctypes.CDLL(str(root / 'auth.so')).libreecho_auth_password
            auth.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            auth.restype = ctypes.c_int
            salt = 'a1' * 16
            password = b'synthetic-test-only'
            digest = hashlib.sha256(salt.encode() + b':' + password).hexdigest()
            valid = f'alice:sha256:{salt}:{digest}\n'
            users.write_text(valid)
            users.chmod(0o600)
            self.assertEqual(auth(b'ALICE', password, len(password)), 1)
            self.assertEqual(auth(b'alice', b'wrong', 5), 0)
            self.assertEqual(auth(b'missing', password, len(password)), 0)
            for content in [valid + valid, valid.replace('alice', 'root'), 'malformed\n', '']:
                users.write_text(content)
                self.assertEqual(auth(b'alice', password, len(password)), 0)
            users.write_text(valid)
            users.chmod(0o644)
            self.assertEqual(auth(b'alice', password, len(password)), 0)

    def test_adb_helper_control_paths_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb = root / 'adb'
            adb.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['FAKE_ROOT'])
a = sys.argv[3:]
with (root/'calls').open('a') as log: log.write(' '.join(a)+'\\n')
if a[0] == 'get-state': print('device')
elif a[0] == 'push' and a[2] == '/run/libreecho-control/runme':
    if os.environ.get('FAKE_TIMEOUT'): sys.exit(0)
    p = subprocess.run(['sh', a[1]], capture_output=True)
    (root/'result').write_bytes(p.stdout + p.stderr)
elif a[0] == 'push' and a[2] == '/run/libreecho-control/runme.cancel': pass
elif a[0] == 'pull' and a[1] == '/run/libreecho-control/result' and (root/'result').exists():
    pathlib.Path(a[2]).write_bytes((root/'result').read_bytes())
else: sys.exit(1)
''')
            adb.chmod(0o755)
            script = root / 'script'
            script.write_text('printf "fixture-output\\n"\nexit 7\n')
            env = dict(os.environ, ADB_SERIAL='synthetic', ADB_BIN=str(adb), FAKE_ROOT=tmp)
            command = ['bash', str(ROOT / 'adb-run-root.sh'), str(script), '1']
            p = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 7, p.stderr)
            self.assertIn('fixture-output', p.stdout)
            (root / 'result').unlink()
            p = subprocess.run(command, env=dict(env, FAKE_TIMEOUT='1'), capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 124)
            self.assertIn('/run/libreecho-control/runme.cancel', (root / 'calls').read_text())


if __name__ == '__main__':
    unittest.main()
