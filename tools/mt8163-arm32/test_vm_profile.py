"""Profile parser and opt-in privileged QEMU boot regressions."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

VM = Path(__file__).resolve().parent / 'ota-test-vm'
spec = importlib.util.spec_from_file_location('parse_profile', VM / 'parse_profile.py')
assert spec is not None and spec.loader is not None
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class ProfileTests(unittest.TestCase):
    def test_mock_entrypoint_profile_and_fallback(self):
        source = (VM.parent / 'emulation/entrypoint-mock.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / 'web'
            web.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            web.chmod(0o755)
            source = source.replace('/usr/local/sbin/libreecho-web', str(web))
            source = source.replace('/data/libreecho/config', str(root / 'config'))
            source = source.replace('/run/libreecho /var/log', str(root / 'run'))
            source = source.replace('/etc/libreecho/web-config.json', str(root / 'default.json'))
            (root / 'default.json').write_text('{}')
            profile = root / 'profile.json'
            for present in [False, True]:
                if present: profile.write_text('{}')
                result = subprocess.run(['sh', '-c', source], env=dict(os.environ, LE_MOCK_PROFILE=str(profile)), capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = result.stdout.splitlines()[1:]
                self.assertEqual('--mock-config' in args, present)
                if present: self.assertEqual(args[args.index('--mock-config') + 1], str(profile))
                self.assertIn('--users-file', args)

    def test_format_fallback_detaches_once(self):
        source = (VM / 'mkdisk.sh').read_text()
        start = source.index('{ LOOP=$(losetup')
        block = source[start:source.index('}', start) + 1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = 'LOOP=; START=0; IMG=fixture; ' + """
losetup() { if [ "$1" = -d ]; then [ ! -e "$MARK" ] || return 99; touch "$MARK"; else printf 'fixture-loop'; fi; }
mke2fs() { :; }
trap 'if [ -n "$LOOP" ]; then losetup -d "$LOOP"; fi' EXIT
""" + block
            result = subprocess.run(['sh', '-ec', script], env=dict(os.environ, MARK=str(root / 'detached')), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'detached').exists())

    def test_guest_applets_resolve_inside_image(self):
        source = (VM / 'build-initramfs.sh').read_text()
        start = source.index('for applet in $(')
        block = source[start:source.index('\ndone', start) + len('\ndone')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bin').mkdir()
            (root / 'bin/busybox').write_text('fixture')
            # Execute the real link-creation block with a deterministic applet list.
            block = block.replace('/bin/busybox --list', 'printf "sh\\nfind\\nstat\\n"')
            subprocess.run(['sh', '-ec', block], env=dict(os.environ, R=tmp), check=True, timeout=5)
            for name in ['sh', 'find', 'stat']:
                self.assertEqual((root / 'bin' / name).resolve(), root / 'bin/busybox')

    def test_structural_state_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            profile = {'other': {'current_slot': 'a', 'rollback_available': False},
                       'system_update': {'current_slot': 'b', 'rollback_available': True},
                       'config_export': {'volume': 26}}
            path.write_text(json.dumps(profile, indent=2).replace(': ', ':\n'))
            self.assertEqual(parser.parse_profile(path), ({'volume': 26}, 'b', True))
            for state in [None, {}, {'current_slot': 'c', 'rollback_available': True},
                          {'current_slot': 'b', 'rollback_available': 'true'}]:
                profile['system_update'] = state
                path.write_text(json.dumps(profile))
                with self.assertRaises(ValueError):
                    parser.parse_profile(path)
            path.write_text('{"config_export": []}')
            with self.assertRaises(ValueError):
                parser.parse_profile(path)

    def test_invalid_profile_never_allocates_disk_or_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / 'bin'
            bindir.mkdir()
            for command in ['apt-get', 'truncate', 'sgdisk', 'losetup', 'mount', 'mke2fs']:
                p = bindir / command
                p.write_text('#!/bin/sh\n' + ('exit 0\n' if command == 'apt-get' else 'echo unexpected-allocation >&2; exit 99\n'))
                p.chmod(0o755)
            profile = root / 'bad.json'
            profile.write_text('{"config_export":false}')
            script = (VM / 'mkdisk.sh').read_text().replace('/work/', str(VM) + '/')
            p = subprocess.run(['sh', '-c', script, 'mkdisk.sh', '--profile', str(profile)],
                               env=dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH']),
                               capture_output=True, text=True, timeout=10)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('invalid profile', p.stderr)
            self.assertNotIn('unexpected-allocation', p.stderr)


class BootTests(unittest.TestCase):
    def test_privileged_boot_matrix(self):
        if os.environ.get('LIBREECHO_VM_BOOT_TEST') != '1':
            self.skipTest('privileged QEMU lane unavailable: set LIBREECHO_VM_BOOT_TEST=1 in disposable staged /work environment')
        self.assertEqual(os.geteuid(), 0, 'explicit VM lane requires root in a disposable container')
        self.assertTrue(os.path.samefile(VM / 'mkdisk.sh', '/work/mkdisk.sh'), 'mount the tested VM directory at /work')
        self.assertIsNotNone(shutil.which('qemu-system-arm'))
        for name in ['vmlinuz', 'initramfs.cpio.gz', 'stage/package.tar']:
            self.assertTrue((VM / name).is_file(), 'missing staged VM input: ' + name)
        subprocess.run(['sh', 'vmtest.sh'], cwd=VM, check=True, timeout=600)
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'profile.json'
            profile.write_text(json.dumps({'system_update': {'current_slot': 'b', 'rollback_available': True}, 'config_export': {}}))
            for scenario in ['', 'config-dir', 'stray-data-file']:
                with self.subTest(scenario=scenario):
                    args = ['sh', 'mkdisk.sh', '--profile', str(profile)]
                    if scenario:
                        args += ['--scenario', scenario]
                    subprocess.run(args, cwd=VM, check=True, timeout=180)
                    subprocess.run(['sh', 'boot-test.sh'], cwd=VM, check=True, timeout=240)
                    log = (VM / 'boot-test.log').read_text()
                    self.assertNotIn('ASSERT:', log)
                    self.assertIn('===PHASE_DATA_CONTRACT_END===', log)
                    if scenario:
                        self.assertIn('brick reproduced and rejected; OTA phases skipped', log)
                        self.assertIn('DATA_CLEANUP_CONTRACT_FAILED', log)
                    else:
                        self.assertIn('installed into a; b preserved as bootable rollback', log)
                        self.assertIn('===PHASE_PROFILE_END===', log)
                        self.assertIn('===PHASE3_END===', log)


if __name__ == '__main__':
    unittest.main()
