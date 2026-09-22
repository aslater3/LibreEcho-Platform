#!/usr/bin/env python3
"""Test the actual 0.14 shell/C userdata contracts without touching devices."""
import hashlib
import re
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ('initramfs/libreecho-init', 'initramfs/libreecho-update', 'stage_feature_root.sh')
VALUES = ('2137088', '2153472', '2137087', '2137089', '2153471', '2153473', '',
          '0', '-2153472', '+2153472', '2153472junk', ' 2153472', '2153472 ',
          '02153472', '999999999999999999999999999999999999')


class UserdataShellGeometryTests(unittest.TestCase):
    def test_all_shell_guards_accept_exactly_two_sizes(self):
        for relative in SCRIPTS:
            source = (ROOT / relative).read_text()
            match = re.search(r'\nuserdata_size_supported\(\)\n\{\n.*?\n\}\n', source, re.S)
            self.assertIsNotNone(match, relative)
            for shell in ['/bin/sh']:
                # BusyBox ash and the system POSIX shell are tested separately below.
                for value in VALUES:
                    result = subprocess.run([shell, '-c', match[0] + '\nuserdata_size_supported "$1"', 'test', value], capture_output=True)
                    self.assertEqual(result.returncode == 0, value in ('2137088', '2153472'), (relative, value))
            if Path('/bin/busybox').is_file():
                for value in VALUES:
                    result = subprocess.run(['/bin/busybox', 'sh', '-c', match[0] + '\nuserdata_size_supported "$1"', 'test', value], capture_output=True)
                    self.assertEqual(result.returncode == 0, value in ('2137088', '2153472'), (relative, value))

    def test_helpers_are_wired_into_identity_checks(self):
        expected = {
            'initramfs/libreecho-init': '! userdata_size_supported "$($BB cat "$USERDATA_SYS/size" 2>/dev/null)"',
            'initramfs/libreecho-update': 'userdata_size_supported "$($BB cat "$USERDATA_SYS/size" 2>/dev/null)"',
            'stage_feature_root.sh': 'userdata_size_supported "$($BB cat "$SYS/size" 2>/dev/null)"',
        }
        for path, expression in expected.items():
            source = (ROOT / path).read_text()
            self.assertIn(expression, source)
            self.assertIn("PARTNAME=userdata", source)
            self.assertNotRegex(source, r'\[ "\$\(\$BB cat "\$(?:USERDATA_)?SYS/size" 2>/dev/null\)" [!=]=? 2137088 \]')
            self.assertEqual(subprocess.run(['/bin/sh', '-n', str(ROOT / path)], capture_output=True).returncode, 0)

    def test_init_hash_pins_match_modified_source(self):
        expected = hashlib.sha256((ROOT / 'initramfs/libreecho-init').read_bytes()).hexdigest()
        for name, constant in (('build_recovery_image.py', 'RECOVERY_INIT_SHA256'), ('verify_recovery_image.py', 'INIT_SHA256')):
            source = (ROOT / name).read_text()
            match = re.search(r'^' + constant + r' = "([0-9a-f]{64})"$', source, re.M)
            self.assertIsNotNone(match)
            self.assertEqual(match[1], expected)


@unittest.skipUnless(shutil.which('cc'), 'C contract test requires a host C compiler')
class UserdataBootctlGeometryTests(unittest.TestCase):
    def test_compiled_partition_contract(self):
        source = (ROOT / 'ota/libreecho_bootctl.c').read_text()
        self.assertIn('!partition_sectors_match(contract, text)', source)
        contract = re.search(r'struct partition_contract \{.*?\n\};', source, re.S)[0]
        table = re.search(r'static const struct partition_contract partitions\[\] = \{.*?\n\};', source, re.S)[0]
        helper = re.search(r'static int partition_sectors_match\(.*?\n\}', source, re.S)[0]
        code = '#include <stdio.h>\n#include <string.h>\n' + contract + '\n' + table + '\n' + helper + r'''
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    for (size_t i = 0; i < sizeof(partitions)/sizeof(partitions[0]); ++i)
        printf("%s:%lu:%d\n", partitions[i].name, partitions[i].sectors,
               partition_sectors_match(&partitions[i], argv[1]));
    struct partition_contract wrong = {"/dev/mmcblk0p99", "unused", "userdata", 2137088};
    if (partition_sectors_match(&wrong, "2153472")) return 3;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'test.c').write_text(code)
            compiled = subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', str(root / 'test.c'), '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for value in VALUES + ('1025', '32768', '225280'):
                result = subprocess.run([str(root / 'test'), value], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for line in result.stdout.splitlines():
                    name, standard, accepted = line.split(':')
                    expected = value == standard or (name == 'userdata' and value == '2153472')
                    self.assertEqual(accepted == '1', expected, (name, value))


if __name__ == '__main__':
    if '--require-compiler' in sys.argv:
        sys.argv.remove('--require-compiler')
        if not shutil.which('cc'):
            raise SystemExit('C contract regression requires a host C compiler')
    unittest.main()
