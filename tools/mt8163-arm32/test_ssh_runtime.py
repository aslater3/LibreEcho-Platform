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
# The device runs BusyBox applets, so prefer BusyBox's awk/stat/sync/kill when
# the host has it; fall back to host tools otherwise.
BUSYBOX = shutil.which('busybox')
BUSYBOX_EXEC = ('exec %s "$@"' % BUSYBOX) if BUSYBOX else 'exec "$@"'


class SSHRuntimeTests(unittest.TestCase):
    def test_saved_ssh_switch_is_fail_closed(self):
        source = (ROOT / 'ssh/libreecho-ssh.init').read_text()
        function = source[source.index('ssh_config_enabled()'):source.index('write_static_accounts()')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'web-config.json'
            wrapper = root / 'bb'
            wrapper.write_text('''#!/bin/sh
if [ "$1" = stat ]; then
    case "$3" in
        %u) printf '0\\n'; exit 0;;
        %a) printf '600\\n'; exit 0;;
        %s) printf '128\\n'; exit 0;;
    esac
fi
''' + BUSYBOX_EXEC + '''
''')
            wrapper.chmod(0o755)
            env = dict(os.environ, BB=str(wrapper), CONFIG_FILE=str(config))

            def enabled(content):
                config.write_text(content)
                result = subprocess.run(['sh', '-c', function + '\nssh_config_enabled'],
                                        env=env, capture_output=True, text=True, timeout=5)
                return result.returncode == 0

            self.assertTrue(enabled('{"ssh":true,"api_lan":false}'))
            self.assertFalse(enabled('{"ssh":false,"api_lan":false}'))
            self.assertFalse(enabled('{"api_lan":false}'))
            self.assertFalse(enabled('{"ssh":true,"ssh":false}'))
            config.unlink()
            self.assertFalse(subprocess.run(['sh', '-c', function + '\nssh_config_enabled'],
                                            env=env, timeout=5).returncode == 0)

    def _dropbear_env(self, root):
        """Fake dropbear/dropbearkey plus a stat shim that reports root-owned 0700/0600."""
        bb = root / 'bb'
        bb.write_text("""#!/bin/sh
if [ "$1" = stat ]; then
    case "$3" in
        %u) printf '0\\n'; exit 0;;
        %a) case "$4" in */keydir) printf '700\\n';; *) printf '600\\n';; esac; exit 0;;
    esac
fi
""" + BUSYBOX_EXEC + "\n")
        bb.chmod(0o755)
        keydir = root / 'keydir'
        keydir.mkdir(mode=0o700)
        state = root / 'state'
        state.mkdir()
        gen = root / 'dropbearkey'
        # Writes a different key every call so a regenerated key is detectable.
        gen.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -f ] && f=$2; shift; done\n'
                       'printf "key-%s-%s\\n" "$$" "$(date +%N)" > "$f"\n')
        gen.chmod(0o755)
        drop = root / 'dropbear'
        drop.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -P ] && p=$2; shift; done\n'
                        'printf "%s\\n" "$$" > "$p"\nexec sleep 60\n')
        drop.chmod(0o755)
        return dict(os.environ, BB=str(bb), HOSTKEY_FILE=str(keydir / 'ssh-host-ed25519-key'),
                    STATE_ROOT=str(state), DROPBEAR=str(drop), DROPBEARKEY=str(gen),
                    DROPBEAR_PIDFILE=str(root / 'dropbear.pid'),
                    SUPERVISOR_PIDFILE=str(root / 'sup.pid')), keydir

    def _ssh_functions(self, *names, state=None):
        source = (ROOT / 'ssh/libreecho-ssh.init').read_text()
        if state is not None:
            source = source.replace('/run/libreecho-ssh', str(state))
        out = []
        for name in names:
            start = source.index(name + '()')
            out.append(source[start:source.index('\n}\n', start) + 3])
        return '\n'.join(out)

    def test_host_key_persists_across_restarts_and_empty_key_is_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, keydir = self._dropbear_env(root)
            funcs = self._ssh_functions('dropbear_running', 'stop_dropbear', 'start_dropbear',
                                        state=root / 'state')
            key = keydir / 'ssh-host-ed25519-key'

            def run(action):
                return subprocess.run(['sh', '-c', funcs + '\n' + action], env=env,
                                      capture_output=True, text=True, timeout=30)

            first = run('start_dropbear && stop_dropbear')
            self.assertEqual(first.returncode, 0, first.stderr)
            original = key.read_text()
            self.assertTrue(original.startswith('key-'))
            self.assertEqual(oct(key.stat().st_mode & 0o777), '0o600')
            # A restart (new process, same persistent store) must keep the identity.
            second = run('start_dropbear && stop_dropbear')
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(key.read_text(), original)
            # A power loss can leave a zero-length key; it must be regenerated.
            key.write_text('')
            third = run('start_dropbear && stop_dropbear')
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertTrue(key.read_text().startswith('key-'))
            self.assertNotEqual(key.read_text(), original)
            # No temporary key files are left behind.
            self.assertEqual([p.name for p in keydir.iterdir()], ['ssh-host-ed25519-key'])

    def test_turning_the_switch_off_stops_dropbear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, _ = self._dropbear_env(root)
            funcs = self._ssh_functions('log', 'log_change', 'ssh_config_enabled', 'dropbear_running',
                                        'stop_dropbear', 'start_dropbear', 'clear_runtime_accounts',
                                        'run_supervisor', state=root / 'state')
            config = root / 'web-config.json'
            config.write_text('{"ssh":true}')
            config.chmod(0o600)
            env['CONFIG_FILE'] = str(config)
            script = (funcs + '\nusers_file_ready() { return 0; }\nsync_accounts() { return 0; }\n'
                      'write_status() { return 0; }\nwrite_static_accounts() { return 0; }\n'
                      'HOME_ROOT=' + str(root / 'home') + '\nrun_supervisor\n')
            proc = subprocess.Popen(['sh', '-c', script], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            pidfile = root / 'dropbear.pid'
            try:
                def alive():
                    try:
                        pid = int(pidfile.read_text().split()[0])
                        os.kill(pid, 0)
                        return True
                    except (OSError, ValueError, IndexError):
                        return False

                def wait(want, seconds):
                    deadline = __import__('time').time() + seconds
                    while __import__('time').time() < deadline:
                        if alive() == want:
                            return True
                        __import__('time').sleep(0.2)
                    return alive() == want

                self.assertTrue(wait(True, 10), 'dropbear should start while the switch is on')
                config.write_text('{"ssh":false}')
                self.assertTrue(wait(False, 15), 'dropbear should stop when the switch is turned off')
                config.write_text('{"ssh":true}')
                self.assertTrue(wait(True, 10), 'dropbear should return when the switch is turned on')
            finally:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                subprocess.run(['pkill', '-f', str(root / 'dropbear')], check=False)
                proc.wait(timeout=5)

    def test_dropbear_start_line_uses_only_options_the_build_keeps(self):
        # #300: -E was passed to a Dropbear configured with --disable-syslog,
        # which rejects it, so SSH never started on any image.
        source = (ROOT / 'ssh/libreecho-ssh.init').read_text()
        build = (ROOT / 'ssh/build_dropbear.sh').read_text()
        start = source.index('    $DROPBEAR ')
        line = source[start:source.index('&\n', start)].replace('\\\n', ' ')
        options = {word for word in line.split() if word.startswith('-')}
        self.assertEqual(options, {'-F', '-P', '-r', '-p'})
        removed_by_configure = {'--disable-syslog': '-E'}
        for flag, option in removed_by_configure.items():
            if flag in build:
                self.assertNotIn(option, options, f'{option} is unavailable with {flag}')

    def _run_supervisor_until(self, root, env, drop_body, predicate, seconds=15):
        """Run the real supervisor loop against a fake dropbear; return ssh.status text."""
        drop = Path(env['DROPBEAR'])
        drop.write_text('#!/bin/sh\n' + drop_body)
        drop.chmod(0o755)
        status = root / 'ssh.status'
        funcs = self._ssh_functions('log', 'log_change', 'write_status', 'ssh_config_enabled',
                                    'dropbear_running', 'stop_dropbear', 'start_dropbear',
                                    'dropbear_start_error', 'clear_runtime_accounts',
                                    'run_supervisor', state=root / 'state')
        config = root / 'web-config.json'
        config.write_text('{"ssh":true}')
        config.chmod(0o600)
        env = dict(env, CONFIG_FILE=str(config))
        script = ('STATUS_FILE=' + str(status) + '\n' + funcs +
                  '\nusers_file_ready() { return 0; }\nsync_accounts() { return 0; }\n'
                  'write_static_accounts() { return 0; }\nrun_supervisor\n')
        proc = subprocess.Popen(['sh', '-c', script], env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        import signal
        import time
        try:
            deadline = time.time() + seconds
            text = ''
            while time.time() < deadline:
                text = status.read_text() if status.exists() else ''
                if predicate(text):
                    break
                time.sleep(0.2)
            return text
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            subprocess.run(['pkill', '-f', str(drop)], check=False, timeout=5)
            proc.wait(timeout=5)

    def test_supervisor_reports_ready_only_when_dropbear_runs(self):
        # A fake that rejects -E exactly like the shipped --disable-syslog build.
        like_shipped = ('for a; do [ "$a" = -E ] && { printf "Invalid option -E\\n'
                        'Dropbear server v2026.93 https://matt.ucc.asn.au/dropbear/dropbear.html\\n'
                        'Usage: /sbin/dropbear [options]\\n-b bannerfile\tDisplay the contents\\n" >&2; exit 1; }; done\n'
                        'while [ $# -gt 0 ]; do [ "$1" = -P ] && p=$2; shift; done\n'
                        'printf "%s\\n" "$$" > "$p"\nexec sleep 60\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, _ = self._dropbear_env(root)
            text = self._run_supervisor_until(root, env, like_shipped,
                                              lambda t: 'account_state=' in t)
            self.assertIn('account_state=ready\n', text)
            self.assertNotIn('start_error=', text)
            self.assertEqual(text.splitlines()[0], 'schema=1')

    def test_supervisor_reports_failed_start_with_sanitized_reason(self):
        always_fails = ('printf "Invalid option -E <x>;\\033[0m\\nUsage: /sbin/dropbear [options]\\n'
                        '-b bannerfile\tDisplay the contents\\n" >&2\nexit 1\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, _ = self._dropbear_env(root)
            text = self._run_supervisor_until(root, env, always_fails,
                                              lambda t: 'account_state=' in t)
            self.assertIn('enabled=1\n', text)
            self.assertIn('account_state=start-failed\n', text)
            self.assertNotIn('account_state=ready', text)
            self.assertIn('start_error=Invalid option -E x0m\n', text)

    def test_supervisor_logs_each_state_change_once(self):
        funcs = self._ssh_functions('log_change')
        result = subprocess.run(['sh', '-c', 'log() { printf "%s\\n" "$1"; }\n' + funcs +
                                 '\nlast_logged=\nfor s in start-failed start-failed start-failed started started start-failed; do log_change $s; done'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stdout.split(), ['start-failed', 'started', 'start-failed'])

    def test_web_binding_survives_first_account_lifecycle(self):
        source = (ROOT / 'initramfs/libreecho-init').read_text()
        start = source.index('                # The control plane is intentionally')
        selection = source[start:source.index('                ARGS=', start)]
        for ready in ['false', 'true']:
            result = subprocess.run(['sh', '-ec',
                'log() { :; }; web_users_file_ready() { ' + ready + '; };\n' +
                selection + '\nprintf "%s" "$web_listen"'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '0.0.0.0:8080')

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
                self.assertEqual([r.split(':')[0] for r in records], ['root'] + [n.lower() for n in names])
                return (root / 'state/accounts').read_text()
            first = poll(['Alice', 'BOB'])
            self.assertEqual(first, poll(['ALICE', 'Bob']))
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
